-- Financial documents: policies, statements, pay stubs, tax returns and the like.
-- Only the index lives here. Each uploaded file is encrypted on its own and written to
-- data/documents/; its key is the key_hex below, so the files are useless without the vault.
-- Keeping the bytes out of the vault matters: the whole vault is re-encrypted and rewritten
-- on every save, and a library of PDFs inside it would make every click slow.
CREATE TABLE documents (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL COLLATE NOCASE,
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (name)
);

CREATE TABLE document_versions (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,              -- as uploaded, for the download name
    content_type TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    uploaded_at  TEXT NOT NULL,              -- 'YYYY-MM-DD HH:MM'
    blob_name    TEXT NOT NULL,              -- random name of the file in data/documents/
    key_hex      TEXT NOT NULL,              -- AES-256-GCM key for that one file
    UNIQUE (blob_name)
);

CREATE INDEX idx_document_versions_document ON document_versions(document_id, uploaded_at DESC);
