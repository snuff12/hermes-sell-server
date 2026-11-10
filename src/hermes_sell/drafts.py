"""Private, immutable drafts exchanged between the Hermes plugin and owner API."""

import json
import os
from pathlib import Path
import re
import tempfile
import time

from .authoring import validate_draft
from .contracts import Problem, canonical, digest

MAX_DRAFT_BYTES = 200_000


class DraftStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)

    def save(self, bundle: dict, draft: dict) -> dict:
        validate_draft(draft)
        content = {"bundle": bundle, "draft": draft}
        record = {**content, "draft_id": digest(content), "bundle_digest": digest(bundle), "created_at": int(time.time())}
        encoded = canonical(record).encode()
        if len(encoded) > MAX_DRAFT_BYTES:
            raise Problem("draft_too_large", "The saved draft must not exceed 200 KB.", 413)
        destination = self.directory / (record["draft_id"] + ".json")
        # A complete file becomes visible atomically; an existing immutable draft wins.
        with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".draft-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        return self.get(record["draft_id"])

    def get(self, draft_id: str) -> dict:
        if not isinstance(draft_id, str) or not re.fullmatch(r"[0-9a-f]{64}", draft_id):
            raise Problem("invalid_draft_id", "Use a valid saved draft ID.", 422)
        path = self.directory / (draft_id + ".json")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                data = handle.read(MAX_DRAFT_BYTES + 1)
        except FileNotFoundError as exc:
            raise Problem("draft_not_found", "Saved draft not found.", 404) from exc
        except OSError as exc:
            raise Problem("invalid_draft", "The saved draft cannot be read safely.", 422) from exc
        if len(data) > MAX_DRAFT_BYTES:
            raise Problem("draft_too_large", "The saved draft must not exceed 200 KB.", 413)
        try:
            record = json.loads(data)
            content = {"bundle": record["bundle"], "draft": record["draft"]}
            if record["draft_id"] != draft_id or digest(content) != draft_id or digest(record["bundle"]) != record["bundle_digest"]:
                raise ValueError("Draft digest mismatch")
            validate_draft(record["draft"])
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise Problem("invalid_draft", "The saved draft failed its integrity check.", 422) from exc
        return record
