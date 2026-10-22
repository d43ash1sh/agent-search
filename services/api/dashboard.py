from __future__ import annotations

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Search & Crawl Engine</title>
    <style>
        :root {
            --bg: #0f172a;
            --surface: #1e293b;
            --surface-hover: #334155;
            --border: #334155;
            --accent: #38bdf8;
            --accent-glow: rgba(56, 189, 248, 0.25);
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --success: #34d399;
            --warning: #fbbf24;
            --danger: #f87171;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.5;
            min-height: 100vh;
            padding: 1.5rem;
        }

        .container { max-width: 1200px; margin: 0 auto; }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--border);
            margin-bottom: 2rem;
        }

        .logo { display: flex; align-items: center; gap: 0.75rem; }
        .logo-icon {
            width: 40px; height: 40px; background: linear-gradient(135deg, #0284c7, #38bdf8);
            border-radius: 10px; display: flex; align-items: center; justify-content: center;
            font-size: 1.25rem; font-weight: bold; color: white;
            box-shadow: 0 0 15px var(--accent-glow);
        }
        .logo-text h1 { font-size: 1.25rem; font-weight: 700; }
        .logo-text p { font-size: 0.8rem; color: var(--text-muted); }

        .header-links { display: flex; gap: 1rem; align-items: center; }
        .header-links a {
            color: var(--accent); text-decoration: none; font-size: 0.875rem;
            padding: 0.4rem 0.8rem; border-radius: 6px; border: 1px solid var(--border);
            transition: all 0.2s;
        }
        .header-links a:hover { background: var(--surface-hover); }

        .tabs { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; border-bottom: 1px solid var(--border); }
        .tab-btn {
            background: none; border: none; color: var(--text-muted); font-size: 1rem;
            font-weight: 600; padding: 0.75rem 1.25rem; cursor: pointer;
            border-bottom: 2px solid transparent; transition: all 0.2s;
        }
        .tab-btn:hover { color: var(--text); }
        .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }

        .tab-pane { display: none; }
        .tab-pane.active { display: block; }

        .card {
            background: var(--surface); border: 1px solid var(--border);
            border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem;
        }

        .metrics-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 1rem; margin-bottom: 1.5rem;
        }
        .metric-card {
            background: var(--surface); border: 1px solid var(--border);
            border-radius: 10px; padding: 1.2rem; display: flex; flex-direction: column;
        }
        .metric-title { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }
        .metric-value { font-size: 1.8rem; font-weight: 700; color: var(--accent); margin-top: 0.25rem; }

        .search-box { display: flex; gap: 0.75rem; margin-bottom: 1rem; }
        .search-input {
            flex: 1; background: #0b1120; border: 1px solid var(--border);
            color: var(--text); padding: 0.85rem 1.2rem; border-radius: 8px; font-size: 1rem;
            outline: none; transition: border-color 0.2s;
        }
        .search-input:focus { border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }

        .btn {
            background: var(--accent); color: #0f172a; font-weight: 600; border: none;
            padding: 0.85rem 1.5rem; border-radius: 8px; cursor: pointer; transition: opacity 0.2s;
            display: inline-flex; align-items: center; justify-content: center; gap: 0.5rem;
        }
        .btn:hover { opacity: 0.9; }

        .controls-row {
            display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center;
            background: #131d31; padding: 0.75rem 1rem; border-radius: 8px; margin-bottom: 1.5rem;
            font-size: 0.875rem;
        }
        .control-group { display: flex; align-items: center; gap: 0.5rem; }
        .control-group label { color: var(--text-muted); }
        .control-group select, .control-group input[type="range"] {
            background: var(--surface); color: var(--text); border: 1px solid var(--border);
            padding: 0.3rem 0.6rem; border-radius: 4px; outline: none;
        }

        .results-section { margin-top: 1.5rem; }
        .hit-card {
            background: #141f35; border: 1px solid var(--border); border-radius: 8px;
            padding: 1.25rem; margin-bottom: 1rem; transition: transform 0.15s, border-color 0.15s;
        }
        .hit-card:hover { border-color: #475569; }
        .hit-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.5rem; }
        .hit-title { font-size: 1.05rem; font-weight: 600; color: #e2e8f0; text-decoration: none; }
        .hit-title:hover { color: var(--accent); }
        .hit-url { font-size: 0.75rem; color: var(--text-muted); word-break: break-all; margin-top: 0.2rem; }
        .hit-badge {
            background: #0284c7; color: white; padding: 0.2rem 0.5rem;
            border-radius: 4px; font-size: 0.75rem; font-weight: 600; font-family: var(--font-mono);
        }
        .hit-snippet {
            font-family: var(--font-mono); font-size: 0.85rem; color: #cbd5e1;
            background: #0b1120; padding: 0.75rem; border-radius: 6px; white-space: pre-wrap;
            margin-top: 0.75rem; max-height: 250px; overflow-y: auto;
        }

        .textarea-box {
            width: 100%; background: #0b1120; border: 1px solid var(--border); color: var(--text);
            padding: 0.85rem; border-radius: 8px; font-family: var(--font-mono); font-size: 0.9rem;
            resize: vertical; min-height: 120px; margin-bottom: 1rem; outline: none;
        }
        .textarea-box:focus { border-color: var(--accent); }

        .alert {
            padding: 0.85rem 1.2rem; border-radius: 8px; margin-bottom: 1rem;
            font-size: 0.875rem; display: none;
        }
        .alert.success { background: rgba(52, 211, 153, 0.15); border: 1px solid var(--success); color: var(--success); display: block; }
        .alert.error { background: rgba(248, 113, 113, 0.15); border: 1px solid var(--danger); color: var(--danger); display: block; }

        .code-block {
            background: #0b1120; border: 1px solid var(--border); border-radius: 6px;
            padding: 1rem; font-family: var(--font-mono); font-size: 0.85rem; color: #e2e8f0;
            overflow-x: auto; white-space: pre; margin-top: 1rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo">
                <div class="logo-icon">⚡</div>
                <div class="logo-text">
                    <h1>Agent Search Engine</h1>
                    <p>Decoupled Crawler • Trafilatura Markdown • FlashRank Reranker • MCP Server</p>
                </div>
            </div>
            <div class="header-links">
                <a href="/docs" target="_blank">Swagger API</a>
                <a href="/openapi.json" target="_blank">OpenAPI JSON</a>
            </div>
        </header>

        <!-- Metrics Overview -->
        <div class="metrics-grid">
            <div class="metric-card">
                <span class="metric-title">Documents Indexed</span>
                <span class="metric-value" id="stat-docs">-</span>
            </div>
            <div class="metric-card">
                <span class="metric-title">Passages Chunked</span>
                <span class="metric-value" id="stat-passages">-</span>
            </div>
            <div class="metric-card">
                <span class="metric-title">Unique Domains</span>
                <span class="metric-value" id="stat-domains">-</span>
            </div>
            <div class="metric-card">
                <span class="metric-title">Queue Backlog</span>
                <span class="metric-value" id="stat-queue">-</span>
            </div>
        </div>

        <!-- Navigation Tabs -->
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('search')">🔍 Search Playground</button>
            <button class="tab-btn" onclick="switchTab('crawl')">🕷️ Dispatch Scraper</button>
            <button class="tab-btn" onclick="switchTab('queues')">📊 Queue Telemetry</button>
        </div>

        <!-- TAB 1: Search Playground -->
        <div id="tab-search" class="tab-pane active">
            <div class="card">
                <div class="search-box">
                    <input type="text" id="search-query" class="search-input" placeholder="Search documentation (e.g. playwright browser context, fastapi background tasks)..." />
                    <button class="btn" onclick="runSearch()">Search</button>
                </div>
                <div class="controls-row">
                    <div class="control-group">
                        <label for="search-limit">Limit:</label>
                        <select id="search-limit">
                            <option value="3">3</option>
                            <option value="5" selected>5</option>
                            <option value="10">10</option>
                            <option value="20">20</option>
                        </select>
                    </div>
                    <div class="control-group">
                        <label for="search-tokens">Token Budget: <span id="token-val">1200</span></label>
                        <input type="range" id="search-tokens" min="400" max="4000" step="100" value="1200" oninput="document.getElementById('token-val').innerText = this.value" />
                    </div>
                    <div class="control-group">
                        <label>
                            <input type="checkbox" id="search-rerank" checked /> Neural Rerank (FlashRank)
                        </label>
                    </div>
                </div>

                <div id="search-results" class="results-section">
                    <p style="color: var(--text-muted); text-align: center; padding: 2rem;">Type a query above to test live retrieval and neural reranking.</p>
                </div>
            </div>
        </div>

        <!-- TAB 2: Dispatch Scraper -->
        <div id="tab-crawl" class="tab-pane">
            <div class="card">
                <h3 style="margin-bottom: 0.75rem;">Enqueue URLs for Distributed Crawler</h3>
                <p style="color: var(--text-muted); font-size: 0.875rem; margin-bottom: 1rem;">
                    Submit one or more URLs (one per line). The fetcher service will process them via HTTP/2 or Playwright, extract clean Markdown, and update the search index.
                </p>
                <div id="crawl-alert" class="alert"></div>
                <textarea id="crawl-urls" class="textarea-box" placeholder="https://example.com/docs&#10;https://fastapi.tiangolo.com/tutorial/"></textarea>
                <div class="controls-row">
                    <div class="control-group">
                        <label><input type="checkbox" id="crawl-render-js" /> Render JavaScript (Force Playwright)</label>
                    </div>
                    <div class="control-group">
                        <label for="crawl-priority">Priority:</label>
                        <select id="crawl-priority">
                            <option value="0" selected>Normal (0)</option>
                            <option value="10">High (10)</option>
                        </select>
                    </div>
                    <button class="btn" onclick="submitCrawl()">Dispatch to Queue</button>
                </div>
            </div>
        </div>

        <!-- TAB 3: Queue Telemetry -->
        <div id="tab-queues" class="tab-pane">
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                    <h3>Real-Time Queue Telemetry</h3>
                    <button class="btn" style="padding: 0.5rem 1rem;" onclick="loadQueueStatus()">Refresh Now</button>
                </div>
                <div id="queue-status-box" class="code-block">Loading telemetry...</div>
            </div>
        </div>
    </div>

    <script>
        function switchTab(name) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById('tab-' + name).classList.add('active');
            if (name === 'queues') loadQueueStatus();
        }

        async function fetchStats() {
            try {
                const res = await fetch('/v1/stats');
                const data = await res.json();
                document.getElementById('stat-docs').innerText = (data.total_documents || 0).toLocaleString();
                document.getElementById('stat-passages').innerText = (data.total_passages || 0).toLocaleString();
                document.getElementById('stat-domains').innerText = (data.total_domains || 0).toLocaleString();
            } catch (e) {
                console.error("Failed to load stats", e);
            }
        }

        async function loadQueueStatus() {
            try {
                const res = await fetch('/v1/queue/status');
                const data = await res.json();
                document.getElementById('queue-status-box').innerText = JSON.stringify(data, null, 2);
                let totalQueue = 0;
                if (data.queues) {
                    totalQueue = Object.values(data.queues).reduce((a, b) => a + b, 0);
                }
                document.getElementById('stat-queue').innerText = totalQueue;
            } catch (e) {
                document.getElementById('queue-status-box').innerText = "Telemetry unavailable (using local queue).";
            }
        }

        async function runSearch() {
            const query = document.getElementById('search-query').value.trim();
            if (!query) return;

            const limit = parseInt(document.getElementById('search-limit').value, 10);
            const tokens = parseInt(document.getElementById('search-tokens').value, 10);
            const rerank = document.getElementById('search-rerank').checked;

            const resultsBox = document.getElementById('search-results');
            resultsBox.innerHTML = '<p style="color: var(--text-muted); padding: 1.5rem; text-align: center;">Searching and neural reranking...</p>';

            try {
                const res = await fetch('/v1/search/grounded', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query, limit, tokens, rerank })
                });
                const data = await res.json();

                if (!data.results || data.results.length === 0) {
                    resultsBox.innerHTML = '<p style="color: var(--text-muted); padding: 1.5rem; text-align: center;">No matching passages found. Try a different query.</p>';
                    return;
                }

                let html = `<div style="margin-bottom: 1rem; color: var(--text-muted); font-size: 0.85rem;">
                    Found <strong>${data.results.length}</strong> evidence passages (${data.estimated_tokens} tokens)
                </div>`;

                data.results.forEach((hit, idx) => {
                    const score = (hit.score || 0).toFixed(4);
                    html += `
                        <div class="hit-card">
                            <div class="hit-header">
                                <div>
                                    <a class="hit-title" href="${hit.url}" target="_blank">[${idx + 1}] ${hit.title || hit.url}</a>
                                    <div class="hit-url">${hit.url}</div>
                                </div>
                                <span class="hit-badge">${score}</span>
                            </div>
                            <div class="hit-snippet">${escapeHtml(hit.text)}</div>
                        </div>
                    `;
                });

                resultsBox.innerHTML = html;
            } catch (e) {
                resultsBox.innerHTML = `<p style="color: var(--danger); padding: 1rem;">Search failed: ${e.message}</p>`;
            }
        }

        async function submitCrawl() {
            const raw = document.getElementById('crawl-urls').value.trim();
            const alertBox = document.getElementById('crawl-alert');
            alertBox.className = 'alert';
            alertBox.style.display = 'none';

            if (!raw) return;
            const urls = raw.split('\\n').map(u => u.trim()).filter(Boolean);
            const renderJs = document.getElementById('crawl-render-js').checked;
            const priority = parseInt(document.getElementById('crawl-priority').value, 10);

            try {
                const res = await fetch('/v1/crawl', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ urls, render_js: renderJs, priority })
                });
                const data = await res.json();
                alertBox.className = 'alert success';
                alertBox.innerText = `Successfully enqueued ${data.enqueued} URL(s) to fetch pipeline!`;
                document.getElementById('crawl-urls').value = '';
                fetchStats();
                loadQueueStatus();
            } catch (e) {
                alertBox.className = 'alert error';
                alertBox.innerText = `Failed to enqueue URLs: ${e.message}`;
            }
        }

        function escapeHtml(str) {
            return str
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }

        document.getElementById('search-query').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') runSearch();
        });

        fetchStats();
        loadQueueStatus();
        setInterval(fetchStats, 6000);
        setInterval(loadQueueStatus, 6000);
    </script>
</body>
</html>
"""
