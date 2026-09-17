# KaiserChat Prototype v0.1

Minimalistischer KaiserSoft AI-Chat als separates Projekt im Website-Repository.

## Struktur

- `public/` – Frontend: KaiserChat UI, animierter Sternenhimmel und Chatlogik
- `api/chat.js` – serverseitiger OpenAI-Responses-API-Endpoint
- `api/credits.js` – serverseitiger API-Credit-Tacho über die dokumentierte OpenAI Usage/Costs API
- `package.json` – OpenAI SDK + Vercel Runtime
- `vercel.json` – Routing für Frontend und API

## Lokaler Betrieb

1. `npm install`
2. `vercel dev`
3. `OPENAI_API_KEY` als Umgebungsvariable setzen.

Optional kann über `KAISERCHAT_MODEL` ein anderes API-Modell gesetzt werden. Standard ist `gpt-5-mini`.

Der API-Key wird ausschließlich serverseitig verwendet und niemals an den Browser ausgeliefert.

## API-Credit-Tacho

Für den echten serverseitigen Tacho werden zusätzlich benötigt:

- `OPENAI_ADMIN_KEY` – OpenAI Admin API Key, ausschließlich serverseitig
- `KAISERCHAT_CREDIT_BUDGET_USD` – das zu beobachtende Kreditbudget, z. B. `5`
- `KAISERCHAT_CREDIT_START_TIME` – Unix-Zeitstempel, ab dem dieses Kreditbudget betrachtet wird
- optional `KAISERCHAT_PROJECT_ID` – OpenAI Project ID, wenn ausschließlich KaiserChat gemessen werden soll

OpenAI stellt über die dokumentierte Costs API die tatsächlichen Kosten bereit. Der historische Prepaid-Credit-Saldo selbst ist nicht als öffentliche Standard-API verfügbar. Deshalb berechnet KaiserChat den Tacho transparent aus dem konfigurierten Kreditbudget minus den von OpenAI gemeldeten Kosten im definierten Zeitraum. Die Kostenabfrage kann laut OpenAI einige Minuten hinter der tatsächlichen Nutzung liegen.
