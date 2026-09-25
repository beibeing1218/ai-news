# AI 新聞看板

抓國外權威媒體與官方部落格的 AI 新聞 RSS，用免費的 Google 翻譯轉成中文，產生一頁看板。
全程不呼叫 AI 模型、不消耗 token，只用 Python 標準庫。

- 雲端版：GitHub Actions 每天台灣時間早上 8 點自動更新，發布到 GitHub Pages。
- 手動更新：到 Actions →「更新 AI 新聞看板」→ Run workflow。
- 本機版：`python ai_news.py open`（網頁上的「立即更新」可直接用）。

來源清單在 `ai_news.py` 最上方的 `FEEDS`。
