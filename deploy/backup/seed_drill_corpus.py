"""Write a small corpus for the restore drill to lose and get back.

Rows and blobs only -- no parsing, no embedding model. The drill is a test of the storage
layer, and making it depend on a multi-gigabyte model download would mean it either runs rarely
or gets switched off. The vectors are deterministic nonsense of the right dimension, which is
all the drill's comparison needs; what it actually proves is that every row, every generated
column and every object key comes back.
"""

import json
import uuid

from sqlalchemy import delete

from src.core.config import SETTINGS
from src.storage import objects
from src.storage.db import session
from src.storage.models import Chunk, Document, IndexAlias, Tenant, User

TENANT_ID = "00000000-0000-0000-0000-00000000dr11"
USER_ID = "00000000-0000-0000-0000-00000000us11"
DOC_ID = "drill" + "0" * 59

PASSAGES = [
    "Employees accrue annual leave each pay period, to a maximum of twenty-five days.",
    "Sick leave is tracked separately from annual leave and does not roll over.",
    "Unused annual leave is paid out on termination at the employee's final rate.",
]
DIM = SETTINGS.storage.embedding_dim
BUILD = SETTINGS.ingest_version


def seed() -> None:
    objects.ensure_bucket()
    with session() as sess:
        sess.execute(delete(Document).where(Document.doc_id == DOC_ID))
        sess.execute(delete(Tenant).where(Tenant.tenant_id == TENANT_ID))
        sess.add(Tenant(tenant_id=TENANT_ID, name=f"drill-{uuid.uuid4().hex[:8]}"))
        sess.add(
            User(
                user_id=USER_ID,
                tenant_id=TENANT_ID,
                email=f"drill-{uuid.uuid4().hex[:8]}@example.test",
                password_hash="not-a-real-hash",
                role="admin",
            )
        )
        sess.flush()
        sess.add(
            Document(
                doc_id=DOC_ID,
                tenant_id=TENANT_ID,
                owner_id=USER_ID,
                filename="leave_policy.txt",
                ingest_version=BUILD,
                pages=1,
                elements_by_kind={"paragraph": len(PASSAGES)},
                num_parents=1,
                num_children=len(PASSAGES),
                state="live",
                version=1,
            )
        )
        sess.flush()
        for ordinal, passage in enumerate(PASSAGES):
            sess.add(
                Chunk(
                    chunk_id=f"{DOC_ID}@{BUILD}:c{ordinal}",
                    doc_id=DOC_ID,
                    tenant_id=TENANT_ID,
                    ingest_version=BUILD,
                    parent_id=f"{DOC_ID}@{BUILD}:p0",
                    ordinal=ordinal,
                    filename="leave_policy.txt",
                    text=passage,
                    embed_text=passage,
                    lexical_text=f"leave_policy.txt\n{passage}",
                    page_start=1,
                    page_end=1,
                    section_path=["Leave"],
                    kind="prose",
                    char_span_in_parent=[0, len(passage)],
                    embedding=[((ordinal + 1) % 7) / 7.0] * DIM,
                )
            )
        sess.merge(IndexAlias(name="active", ingest_version=BUILD))

    objects.put_raw(DOC_ID, "leave_policy.txt", "\n\n".join(PASSAGES).encode())
    objects.put_parents(
        DOC_ID,
        BUILD,
        json.dumps(
            {
                "parents": [
                    {
                        "parent_id": f"{DOC_ID}@{BUILD}:p0",
                        "doc_id": DOC_ID,
                        "filename": "leave_policy.txt",
                        "text": "\n\n".join(PASSAGES),
                        "page_start": 1,
                        "page_end": 1,
                        "section_path": ["Leave"],
                        "kind": "prose",
                    }
                ]
            }
        ).encode(),
    )
    print(f"Seeded {len(PASSAGES)} chunks for the drill.")


if __name__ == "__main__":
    seed()
