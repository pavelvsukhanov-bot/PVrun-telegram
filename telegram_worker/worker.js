// Cloudflare Worker: Telegram webhook → GitHub Actions.
// "?" from the owner's chat starts the "Daily Training Plan" workflow and the
// bot answers right away (a reply in the webhook response, so the Worker
// does not need the bot token). Everything else is ignored.
//
// Worker variables (Settings → Variables and Secrets):
//   GH_TOKEN        fine-grained GitHub token, Actions: Read and write on PVrun-telegram (secret)
//   WEBHOOK_SECRET  same value as secret_token passed to Telegram setWebhook (secret)
//   CHAT_ID         Telegram chat id allowed to request plans

const DISPATCH_URL =
  "https://api.github.com/repos/pavelvsukhanov-bot/PVrun-telegram/actions/workflows/daily_plan.yml/dispatches";

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    const update = await request.json();
    const msg = update.message;
    const text = (msg?.text || "").trim();
    if (!msg || String(msg.chat.id) !== env.CHAT_ID || (text !== "?" && text !== "？")) {
      return new Response("ok");
    }

    const gh = await fetch(DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pvrun-telegram-worker",   // GitHub rejects requests without one
      },
      body: JSON.stringify({ ref: "main" }),
    });

    const reply = gh.status === 204
      ? "⏳ Составляю план на сегодня, около минуты…"
      : `⚠️ Не удалось запустить план (GitHub ответил ${gh.status}). Проверь GH_TOKEN в Cloudflare — возможно, истёк.`;
    return Response.json({ method: "sendMessage", chat_id: msg.chat.id, text: reply });
  },
};
