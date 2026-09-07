import tempfile
import unittest
from pathlib import Path

from session_token_viewer import files_from_content, mentioned_files


class FileAttachmentTests(unittest.TestCase):
    def test_codex_file_mention_loads_local_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.txt"
            path.write_text("attached content", encoding="utf-8")
            files = mentioned_files(f"# Files mentioned by the user:\n\n## notes.txt: {path}")
        self.assertEqual(files[0]["name"], "notes.txt")
        self.assertEqual(files[0]["content"], "attached content")

    def test_structured_document_content_is_normalized(self) -> None:
        files = files_from_content([
            {"type": "document", "name": "brief.md", "source": {"data": "# Brief"}}
        ])
        self.assertEqual(files, [{"name": "brief.md", "path": None, "content": "# Brief"}])

    def test_xml_attachment_keeps_embedded_content(self) -> None:
        files = mentioned_files('<attachment id="README.md" filePath="C:\\repo\\README.md">Hello</attachment>')
        self.assertEqual(files[0]["name"], "README.md")
        self.assertEqual(files[0]["content"], "Hello")


if __name__ == "__main__":
    unittest.main()
