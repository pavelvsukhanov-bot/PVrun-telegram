// Cloudflare Worker: Telegram webhook → GitHub Actions.
//   "?" from the owner's chat          → "Daily Training Plan" workflow
//   "⌚ Отправить на часы" button tap  → "Send Workout to Watch" workflow
// The bot answers in the webhook response itself, so the Worker does not
// need the bot token. Everything else is ignored.
//
// Worker variables (Settings → Variables and Secrets):
//   GH_TOKEN        fine-grained GitHub token, Actions: Read and write on PVrun-telegram (secret)
//   WEBHOOK_SECRET  same value as secret_token passed to Telegram setWebhook (secret)
//   CHAT_ID         Telegram chat id allowed to use the bot

const WORKFLOWS =
  "https://api.github.com/repos/pavelvsukhanov-bot/PVrun-telegram/actions/workflows/";

async function dispatch(env, workflow, inputs) {
  const resp = await fetch(WORKFLOWS + workflow + "/dispatches", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "pvrun-telegram-worker",   // GitHub rejects requests without one
    },
    body: JSON.stringify({ ref: "main", ...(inputs ? { inputs } : {}) }),
  });
  return resp.status;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json();

    // Button under the plan: the workout code travels in callback_data ("W:<code>")
    const cq = update.callback_query;
    if (cq) {
      const msg = cq.message;
      if (!msg || String(msg.chat.id) !== env.CHAT_ID || !(cq.data || "").startsWith("W:")) {
        return Response.json({ method: "answerCallbackQuery", callback_query_id: cq.id });
      }
      const status = await dispatch(env, "send_workout.yml", {
        code: cq.data.slice(2),
        msg_date: String(msg.date),
        message_id: String(msg.message_id),
      });
      return Response.json(status === 204
        ? { method: "answerCallbackQuery", callback_query_id: cq.id, text: "⌚ Отправляю на часы…" }
        : { method: "answerCallbackQuery", callback_query_id: cq.id, show_alert: true,
            text: `⚠️ Не удалось запустить отправку (GitHub ${status}). Проверь GH_TOKEN в Cloudflare.` });
    }

    const msg = update.message;
    const text = (msg?.text || "").trim();
    if (!msg || String(msg.chat.id) !== env.CHAT_ID || (text !== "?" && text !== "？")) {
      return new Response("ok");
    }
    const status = await dispatch(env, "daily_plan.yml");
    const reply = status === 204
      ? "⏳ Составляю план на сегодня, около минуты…"
      : `⚠️ Не удалось запустить план (GitHub ответил ${status}). Проверь GH_TOKEN в Cloudflare — возможно, истёк.`;
    return Response.json({ method: "sendMessage", chat_id: msg.chat.id, text: reply });
  },
};
