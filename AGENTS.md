# Channel Vault project instructions

- After every completed UI or feature change, show the user an updated interactive preview of the finished pages in the final response. Include the preview even when also providing a ZIP or a GitHub link.
- Keep preview data explicitly marked as demonstration data. Match the current implementation. Do not imply live website downloads were tested when only mocked or local conversion tests ran.
- Preserve highest-quality original downloads, MP3 copies, compatible playback copies, and all image history.
- Keep credentials, cookies, API tokens, local .env files, SQLite data, and media outside version control. Secret-setting APIs must never return saved secret contents.
- Maintain migration compatibility for existing installations and test important queue, settings, source and HTTP behavior.
- ZIP releases must include explicit empty data/, downloads/ and cookies/ directory entries.
