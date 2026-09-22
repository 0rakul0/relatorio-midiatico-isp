from pathlib import Path

def test_pdf_download_uses_authenticated_fetch():
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "downloadAuthenticatedPdf" in script
    assert "authHeaders()" in script
    assert "window.open(\`/projects/\${currentProjectId}/export.pdf" not in script
