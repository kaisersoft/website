export default async function handler(req, res) {
  if (req.method !== "GET") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const adminKey = process.env.OPENAI_ADMIN_KEY;
  const budgetUsd = Number(process.env.KAISERCHAT_CREDIT_BUDGET_USD || 5);
  const startTime = Number(process.env.KAISERCHAT_CREDIT_START_TIME || 0);
  const projectId = process.env.KAISERCHAT_PROJECT_ID || process.env.OPENAI_PROJECT_ID || "";

  // OpenAI's documented organization cost endpoint requires an admin API key.
  // The public credit-grant balance endpoint is not a supported API surface.
  if (!adminKey) {
    return res.status(503).json({
      configured: false,
      error: "OPENAI_ADMIN_KEY is not configured."
    });
  }

  if (!Number.isFinite(budgetUsd) || budgetUsd <= 0 || !Number.isFinite(startTime) || startTime <= 0) {
    return res.status(503).json({
      configured: false,
      error: "KAISERCHAT_CREDIT_BUDGET_USD and KAISERCHAT_CREDIT_START_TIME must be configured."
    });
  }

  try {
    const endTime = Math.floor(Date.now() / 1000);
    let page;
    let usedUsd = 0;
    let requestCount = 0;

    do {
      const costParams = new URLSearchParams({
        start_time: String(startTime),
        end_time: String(endTime),
        bucket_width: "1d",
        limit: "180"
      });
      if (projectId) costParams.append("project_ids", projectId);
      if (page) costParams.set("page", page);

      const costResponse = await fetch(`https://api.openai.com/v1/organization/costs?${costParams.toString()}`, {
        headers: {
          Authorization: `Bearer ${adminKey}`,
          "Content-Type": "application/json"
        }
      });

      if (!costResponse.ok) {
        const detail = await costResponse.text();
        console.error("KaiserChat credits cost API error:", costResponse.status, detail);
        return res.status(502).json({ configured: false, error: "OpenAI usage data could not be read." });
      }

      const costData = await costResponse.json();
      for (const bucket of costData.data || []) {
        for (const result of bucket.results || []) {
          if (result.amount?.currency === "usd") {
            usedUsd += Number(result.amount.value) || 0;
          }
        }
      }
      page = costData.next_page || null;
    } while (page);

    // The completions usage endpoint provides a real request count for the same range.
    let usagePage;
    do {
      const usageParams = new URLSearchParams({
        start_time: String(startTime),
        end_time: String(endTime),
        bucket_width: "1d",
        limit: "31"
      });
      if (projectId) usageParams.append("project_ids", projectId);
      if (usagePage) usageParams.set("page", usagePage);

      const usageResponse = await fetch(`https://api.openai.com/v1/organization/usage/completions?${usageParams.toString()}`, {
        headers: {
          Authorization: `Bearer ${adminKey}`,
          "Content-Type": "application/json"
        }
      });

      if (!usageResponse.ok) break;
      const usageData = await usageResponse.json();
      for (const bucket of usageData.data || []) {
        for (const result of bucket.results || []) {
          requestCount += Number(result.num_model_requests) || 0;
        }
      }
      usagePage = usageData.next_page || null;
    } while (usagePage);

    const remainingUsd = Math.max(0, budgetUsd - usedUsd);
    const usedPercent = Math.min(100, Math.max(0, (usedUsd / budgetUsd) * 100));
    const avgUsd = requestCount > 0 ? usedUsd / requestCount : 0;

    res.setHeader("Cache-Control", "s-maxage=60, stale-while-revalidate=120");
    return res.status(200).json({
      configured: true,
      source: "openai-usage-api",
      budgetUsd: Number(budgetUsd.toFixed(6)),
      usedUsd: Number(usedUsd.toFixed(6)),
      remainingUsd: Number(remainingUsd.toFixed(6)),
      usedPercent: Number(usedPercent.toFixed(3)),
      requests: requestCount,
      avgUsd: Number(avgUsd.toFixed(8)),
      updatedAt: new Date().toISOString(),
      projectFiltered: Boolean(projectId)
    });
  } catch (error) {
    console.error("KaiserChat credits error:", error);
    return res.status(502).json({ configured: false, error: "KaiserChat konnte die OpenAI-Nutzungsdaten gerade nicht laden." });
  }
}
