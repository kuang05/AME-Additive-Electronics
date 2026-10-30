# -*- coding: utf-8 -*-
"""修 PgStore 下的工程管理：rename 同步 catalog、delete 走 DROP SCHEMA、
copy 走 clone_to_new_schema、open_existing_project 做存在性检查。"""
from pathlib import Path

p = Path("ame/db/store.py")
t = p.read_text(encoding="utf-8")

# ---------- 1) open_existing_project：PG 下也要检查存在 ----------
old = '''        pid = _safe_project_id(project_id)
        if self._store_factory is not None:
            return self._store_factory(pid)
        path = self.data_dir / "projects" / f"{pid}.db"
        if not path.exists():
            raise FileNotFoundError(f"工程 {pid} 不存在")
        return Store(path)'''
new = '''        pid = _safe_project_id(project_id)
        if self._store_factory is not None:
            # PG：构造 PgStore 会 CREATE SCHEMA + 建 26 张表，所以必须先查目录表。
            # 早期这里直接 return factory(pid)，等于"打开不存在的工程就建一个新
            # schema"——把 SQLite 版用 path.exists() 挡住的"扫描器刷爆磁盘"
            # 变成了"刷爆 RDS schema 数量"（每个 8 位 uuid 一个 schema，永不回收）。
            if pid != "__catalog__" and not self._pg_project_exists(pid):
                raise FileNotFoundError(f"工程 {pid} 不存在")
            return self._store_factory(pid)
        path = self.data_dir / "projects" / f"{pid}.db"
        if not path.exists():
            raise FileNotFoundError(f"工程 {pid} 不存在")
        return Store(path)

    def _pg_project_exists(self, pid: str) -> bool:
        """在 `ame_catalog` 目录表里查工程是否存在。"""
        try:
            cat = self._store_factory("__catalog__")
        except Exception:
            return False
        try:
            row = cat.one("SELECT 1 AS x FROM ame_catalog WHERE project_id=?", (pid,))
            return bool(row)
        except Exception:
            return False
        finally:
            with contextlib.suppress(Exception):
                cat.close()'''
assert old in t
t = t.replace(old, new, 1)

# ---------- 2) rename_project：PG 下同步 catalog ----------
old2 = '''        st = self.open_existing_project(project_id)
        st.execute("UPDATE project SET name=? WHERE id=?", (new_name, project_id))
        st.close()
        return new_name'''
new2 = '''        st = self.open_existing_project(project_id)
        st.execute("UPDATE project SET name=? WHERE id=?", (new_name, project_id))
        st.close()
        # PG：目录表是工程列表的唯一来源，改名必须同步，否则列表里还是旧名
        self._pg_catalog_update(project_id, name=new_name)
        return new_name

    def _pg_catalog_update(self, pid: str, name: str | None = None,
                           head: str | None = None) -> None:
        """更新 `ame_catalog` 目录行（SQLite 模式无操作）。"""
        if self._store_factory is None:
            return
        sets, params = [], []
        if name is not None:
            sets.append("name=?")
            params.append(name)
        if head is not None:
            sets.append("head_commit_id=?")
            params.append(head)
        if not sets:
            return
        try:
            cat = self._store_factory("__catalog__")
        except Exception:
            return
        try:
            cat.execute(f"UPDATE ame_catalog SET {', '.join(sets)} "
                        "WHERE project_id=?", (*params, pid))
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                cat.close()

    def _pg_catalog_remove(self, pid: str) -> None:
        if self._store_factory is None:
            return
        try:
            cat = self._store_factory("__catalog__")
        except Exception:
            return
        try:
            cat.execute("DELETE FROM ame_catalog WHERE project_id=?", (pid,))
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                cat.close()'''
assert old2 in t
t = t.replace(old2, new2, 1)

# ---------- 3) copy_project：PG 分支 ----------
old3 = '''        import uuid
        src = self.open_existing_project(project_id)
        src_meta = src.one("SELECT * FROM project")'''
new3 = '''        import uuid
        src = self.open_existing_project(project_id)
        if self._store_factory is not None:
            # PG：没有文件可备份，走 schema 复制
            return self._pg_copy_project(src, project_id, new_name)
        src_meta = src.one("SELECT * FROM project")'''
assert old3 in t
t = t.replace(old3, new3, 1)

# 在 delete_project 前插入 _pg_copy_project
old4 = '''    def delete_project(self, project_id: str) -> dict:'''
new4 = '''    def _pg_copy_project(self, src, project_id: str, new_name: str | None) -> dict:
        """PG 版工程复制：clone_to_new_schema + 登记目录。"""
        meta = src.one("SELECT * FROM project")
        if not meta:
            raise FileNotFoundError(f"工程 {project_id} 不存在或已损坏")
        new_pid = str(uuid.uuid4())
        dst_name = (new_name or "").strip() or f"{meta['name']} 副本"
        clone = src.clone_to_new_schema("copy")
        n_routes = len(clone.query("SELECT id FROM route"))
        n_versions = len(clone.query("SELECT id FROM version"))
        # clone 出来的 schema 名是 "<原id>-copy-xxxx"，主键要改成新 pid
        clone.execute("UPDATE project SET id=?, name=?", (new_pid, dst_name))
        clone.execute("UPDATE ame_catalog SET name=?", (dst_name,)) \
            if False else None
        clone.commit() if hasattr(clone, "commit") else None
        self._register_in_catalog_direct(new_pid, dst_name)
        return {"id": new_pid, "name": dst_name, "routes": n_routes,
                "versions": n_versions}

    def _register_in_catalog_direct(self, pid: str, name: str) -> None:
        if self._store_factory is None:
            return
        try:
            cat = self._store_factory("__catalog__")
        except Exception:
            return
        try:
            cat.execute("INSERT INTO ame_catalog(project_id,name,created_at,"
                        "head_commit_id) VALUES (?,?,?,NULL)",
                        (pid, name, time.strftime("%Y-%m-%dT%H:%M:%S")))
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                cat.close()

    def delete_project(self, project_id: str) -> dict:'''
assert old4 in t
t = t.replace(old4, new4, 1)

# ---------- 4) delete_project：PG 分支 ----------
old5 = '''    def delete_project(self, project_id: str) -> dict:
        """删除工程（PRJ-007：需二次确认——由 UI 层做；先移入回收站，可恢复）。"""
        f = self._project_file(project_id)'''
new5 = '''    def delete_project(self, project_id: str) -> dict:
        """删除工程（PRJ-007：需二次确认——由 UI 层做；先移入回收站，可恢复）。

        PG 模式没有文件可移，改为 `DROP SCHEMA ... CASCADE` 并清理目录表。
        早期 PG 下这里第一步 `_project_file()` 就抛 FileNotFoundError——
        **云上根本没有"删除工程"的能力**。
        """
        if self._store_factory is not None:
            pid = _safe_project_id(project_id)
            if not self._pg_project_exists(pid):
                raise FileNotFoundError(f"工程 {pid} 不存在")
            st = self._store_factory(pid)
            try:
                st.drop_schema()
            finally:
                with contextlib.suppress(Exception):
                    st.close()
            self._pg_catalog_remove(pid)
            return {"dropped_schema": pid, "restore_hint":
                    "PostgreSQL 模式不支持回收站恢复，请事先导出工程包备份"}
        f = self._project_file(project_id)'''
assert old5 in t
t = t.replace(old5, new5, 1)
p.write_text(t, encoding="utf-8")
print("工程管理 PG 分支完成")
