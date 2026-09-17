import OpenAI from "openai";

const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  if (!process.env.OPENAI_API_KEY) {
    return res.status(500).json({ error: "OPENAI_API_KEY is not configured." });
  }

  try {
    const { messages } = req.body || {};
    if (!Array.isArray(messages) || messages.length === 0) {
      return res.status(400).json({ error: "No messages supplied." });
    }

    const cleaned = messages
      .filter((message) => message && ["user", "assistant"].includes(message.role) && typeof message.content === "string")
      .slice(-20)
      .map((message) => ({ role: message.role, content: message.content.slice(0, 12000) }));

    if (!cleaned.length || cleaned[cleaned.length - 1].role !== "user") {
      return res.status(400).json({ error: "A user message is required." });
    }

    const response = await client.responses.create({
      model: process.env.KAISERCHAT_MODEL || "gpt-5-mini",
      instructions: "You are KaiserChat, a helpful, clear and concise general-purpose AI assistant. Answer in the language of the user. Do not claim to be ChatGPT or an OpenAI product. Be useful without unnecessary complexity.",
      input: cleaned
    });

    return res.status(200).json({
      message: response.output_text || "Ich konnte gerade keine Antwort erzeugen.",
      model: process.env.KAISERCHAT_MODEL || "gpt-5-mini"
    });
  } catch (error) {
    console.error("KaiserChat API error:", error);
    return res.status(500).json({ error: "KaiserChat konnte die Anfrage gerade nicht verarbeiten." });
  }
}
