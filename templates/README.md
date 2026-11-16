# Circus issue email template

`issue-email.html` is the adjustable email layout. `preview-data.json` contains filler text used to generate the persistent `sample-issue.html` and `sample-issue.txt` examples. These samples are **not actual editorial output** and are not sent.

From the project root, regenerate the samples after changing the layout or filler data:

```powershell
& .\.venv-host\Scripts\python.exe scripts\render_preview.py
```

The HTML uses a fluid table that fills the available width up to 960px, with mostly inline CSS. Text wraps within that width, including on phones. The 960px limit keeps lines readable on very wide desktop displays. Noto Sans and Noto Serif match the font families found in the operations PDF; Arial and Georgia are fallbacks for recipients without those fonts. The layout and CSS are meant to be revised after visual review. The operations PDF is a brief and original-submissions archive, not a finished issue email.

The sample has the requested order: masthead with computed issue number, editor-provided title, a strong rule, editor-provided summary, THE FEED, WORTH YOUR TIME, PERSONAL UPDATES, GALLERY, SLEEPLESS’S TAKE, and THE LEDGER. Sleepless’s Take is required for AI drafts and blank in the deterministic fallback; the preview uses filler text. Other optional sections disappear when they have no items. The gallery uses blank boxes only in preview mode. A real render requires each image to have a `cid:` reference or HTTPS URL, plus caption, credit, and alt text.

`circus_newsletter/render.py` escapes issue text, checks link schemes, computes the issue number and next deadline from `config/schedule.json`, and produces both HTML and a plain-text companion. It does not decide what editorial text goes into the sections and does not send email. The future editor should produce reviewed structured content in the same shape as `preview-data.json`; delivery remains a separate later stage.
