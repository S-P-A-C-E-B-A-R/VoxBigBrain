const form = document.getElementById("loginForm");
const error = document.getElementById("loginError");
let csrfToken;
async function csrf() { const response = await fetch("/api/auth/csrf", { cache: "no-store" }); csrfToken = (await response.json()).csrfToken; }
form.addEventListener("submit", async (event) => { event.preventDefault(); error.textContent = ""; const response = await fetch("/api/login", { method: "POST", headers: { "content-type": "application/json", "x-csrf-token": csrfToken }, body: JSON.stringify({ username: form.username.value, password: form.password.value }) }); if (!response.ok) { error.textContent = (await response.json()).error || "Sign-in failed"; return csrf(); } window.location.assign("/"); });
csrf().catch(() => { error.textContent = "Sign-in is temporarily unavailable."; });
