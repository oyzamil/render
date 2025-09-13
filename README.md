# MacTrac Backend (Render-ready)

## Deploy
1. Push this repo to GitHub.
2. Create a new Web Service in Render, connect repo.
3. Render reads `render.yaml` and installs requirements.
4. Set env var `OPENAI_API_KEY` in Render dashboard.

## Test
```bash
curl https://YOUR-SERVICE.onrender.com/health
curl -X POST https://YOUR-SERVICE.onrender.com/api/chat-completions   -H "Content-Type: application/json"   -d '{"messages":[{"role":"system","content":"ping"},{"role":"user","content":"hello"}],"model":"gpt-4o-mini"}'
```
