// Frontend Bubbles. Uses the normal publishable/anon key through your existing Supabase client.
// Never put SUPABASE_SERVICE_ROLE_KEY here.

async function connectTelegramToBubbles(supabase) {
  const { data, error } = await supabase.rpc("create_telegram_link_token");
  if (error) {
    console.error("Telegram link token error:", error);
    alert("Не удалось создать ссылку Telegram.");
    return;
  }
  const telegramUrl =
    "https://t.me/YOUR_BOT_USERNAME?start=" + encodeURIComponent(data);
  window.open(telegramUrl, "_blank", "noopener,noreferrer");
}

// Example:
// document.querySelector('#connect-telegram')?.addEventListener('click', () => connectTelegramToBubbles(supabase));
