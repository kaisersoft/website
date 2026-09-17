# KaiserChat Prototype v0.1

Minimalistischer KaiserSoft AI-Chat als separates Projekt im Website-Repository.

## Struktur

- `public/` – Frontend: KaiserChat UI, animierter Sternenhimmel und Chatlogik
- `api/chat.js` – serverseitiger OpenAI-Responses-API-Endpoint
- `package.json` – OpenAI SDK + Vercel Runtime
- `vercel.json` – Routing für Frontend und API

## Lokaler Betrieb

1. `npm install`
2. `vercel dev`
3. `OPENAI_API_KEY` als Umgebungsvariable setzen.

Optional kann über `KAISERCHAT_MODEL` ein anderes API-Modell gesetzt werden. Standard ist `gpt-5-mini`.

Der API-Key wird ausschließlich serverseitig verwendet und niemals an den Browser ausgeliefert.
