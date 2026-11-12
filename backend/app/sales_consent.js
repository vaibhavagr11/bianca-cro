"use strict";

const page = document.querySelector("main");
const config = JSON.parse(page.dataset.config);
const authorizationId = page.dataset.authorizationId;
const authorizationPath = "/oauth/authorizations/" + encodeURIComponent(authorizationId);
const emailForm = document.querySelector("#send-code");
const codeForm = document.querySelector("#verify-code");
const consent = document.querySelector("#consent");
const email = document.querySelector("#email");
const code = document.querySelector("#code");
const statusMessage = document.querySelector("#status");
const approve = document.querySelector("#approve");
const deny = document.querySelector("#deny");
let accessToken;

async function request(path, body, token) {
  const headers = { apikey: config.publishableKey, Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = "Bearer " + token;
  const response = await fetch(config.issuer + path, {
    method: body === undefined ? "GET" : "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
    headers,
    credentials: "omit",
    redirect: "error",
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error("Supabase Auth request failed");
  return response.json();
}

function returnToClaude(value) {
  if (typeof value !== "string") throw new Error("Missing redirect");
  const target = new URL(value);
  const callback = new URL(config.claudeCallback);
  if (target.origin !== callback.origin || target.pathname !== callback.pathname ||
      target.username || target.password || target.hash) {
    throw new Error("Unexpected OAuth redirect");
  }
  accessToken = undefined;
  window.location.replace(target.href);
}

async function isInvited(subject) {
  if (typeof subject !== "string") return false;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(subject));
  const hash = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
  return config.allowedUserHashes.includes(hash);
}

emailForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = emailForm.querySelector("button");
  button.disabled = true;
  statusMessage.textContent = "";
  try {
    await request("/otp", { email: email.value.trim(), create_user: false });
    statusMessage.textContent = "Check your email and enter the code here.";
    emailForm.hidden = true;
    codeForm.hidden = false;
    code.focus();
  } catch {
    statusMessage.textContent = "The code could not be sent. Check the invited address and try again.";
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#change-email").addEventListener("click", () => {
  codeForm.hidden = true;
  code.value = "";
  emailForm.hidden = false;
  statusMessage.textContent = "";
  email.focus();
});

codeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const buttons = Array.from(codeForm.querySelectorAll("button"));
  buttons.forEach((button) => { button.disabled = true; });
  statusMessage.textContent = "";
  try {
    const verified = await request("/verify", { email: email.value.trim(), token: code.value, type: "email" });
    code.value = "";
    if (typeof verified.access_token !== "string" || !verified.access_token ||
        !await isInvited(verified.user?.id)) {
      statusMessage.textContent = "Use an email address invited to Bianca Sales.";
      return;
    }
    const details = await request(authorizationPath, undefined, verified.access_token);
    // Supabase can return the verified callback directly when this user already granted consent.
    if (details.redirect_url) {
      returnToClaude(details.redirect_url);
      return;
    }
    if (details.authorization_id !== authorizationId || details.client?.id !== config.clientId ||
        details.redirect_uri !== config.claudeCallback || details.user?.id !== verified.user.id) {
      statusMessage.textContent = "Open Bianca Sales in Claude to start a new sign-in request.";
      return;
    }
    accessToken = verified.access_token;
    document.querySelector("#scopes").textContent = "Requested permissions: " + details.scope;
    codeForm.hidden = true;
    consent.hidden = false;
    approve.focus();
  } catch {
    statusMessage.textContent = "Sign-in could not continue. Check your code or reconnect from Claude.";
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
});

async function submitConsent(action) {
  if (!accessToken) return;
  approve.disabled = true;
  deny.disabled = true;
  statusMessage.textContent = "";
  try {
    const result = await request(authorizationPath + "/consent", { action }, accessToken);
    returnToClaude(result.redirect_url);
  } catch {
    statusMessage.textContent = "Claude could not be reached. Return to Claude and reconnect.";
  } finally {
    approve.disabled = false;
    deny.disabled = false;
  }
}

approve.addEventListener("click", () => submitConsent("approve"));
deny.addEventListener("click", () => submitConsent("deny"));
