# ==================== launcher/core/chunk_manifest_db.py ====================
"""
Чтение chunk-манифеста (`manifest.db`, SQLite) — компаньона к обычному
JSON-манифесту версии, для сборок, физически хранящихся на сервере как
content-addressed чанки (`chunks/<xx>/<id>`), а не как плоские файлы
`files/<rel_path>`. См. `config.py` ("Chunk-based версии") — обнаружение
компаньона (по имени рядом с JSON, расширение `.db`) живёт в
`core/depot_client.py::DepotClient.fetch_manifest_db_bytes()`, а сама
загрузка/сборка чанков — в `core/workers.py::DownloadWorker._run_chunk_install()`.

Файл генерируется TESL-Manager'ом (`depot_sync_manager/build_manifest_db.py`)
и НЕ пишется отсюда — только читается. Схема — см. `build_manifest_db.py`'s
собственный докстринг (таблицы `meta`/`files`/`chunks`); держи их в паре в
курсе изменений друг друга, если схема когда-нибудь поменяется.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass
class ChunkInfo:
    chunk_id: str
    offset:   int
    size:     int


@dataclass
class FileEntry:
    component: str
    path:      str
    size:      int
    file_hash: str
    chunks:    List[ChunkInfo]

    @property
    def rel_out_path(self) -> str:
        """Путь относительно local_dir — та же конвенция, что у
        обычного плоского протокола (`files/<Компонент>/<rel_path>`)."""
        return f"{self.component}/{self.path}" if self.component else self.path

    def chunk_ids(self) -> List[str]:
        return [c.chunk_id for c in self.chunks]


def read_manifest_db(db_path: Path) -> Tuple[List[FileEntry], dict]:
    """Возвращает (список файлов, метаданные версии из таблицы meta)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        meta_row = conn.execute("SELECT * FROM meta LIMIT 1").fetchone()
        meta = dict(meta_row) if meta_row else {}

        chunks_by_file: Dict[Tuple[str, str], List[ChunkInfo]] = {}
        for row in conn.execute(
            "SELECT component, path, chunk_id, offset, size FROM chunks ORDER BY component, path, offset"
        ):
            key = (row["component"], row["path"])
            chunks_by_file.setdefault(key, []).append(
                ChunkInfo(row["chunk_id"], row["offset"], row["size"])
            )

        entries: List[FileEntry] = []
        for row in conn.execute("SELECT component, path, size, file_hash FROM files"):
            key = (row["component"], row["path"])
            entries.append(FileEntry(
                component=row["component"],
                path=row["path"],
                size=row["size"],
                file_hash=row["file_hash"],
                chunks=chunks_by_file.get(key, []),
            ))
        return entries, meta
    finally:
        conn.close()
