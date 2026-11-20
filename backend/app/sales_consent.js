"use strict";

const page = document.querySelector("main");
const config = JSON.parse(page.dataset.config);
const authorizationId = page.dataset.authorizationId;
const authorizationPath = "/oauth/authorizations/" + encodeURIComponent(authorizationId);
const signInForm = document.querySelector("#sign-in");
const consent = document.querySelector("#consent");
const email = document.querySelector("#email");
const password = document.querySelector("#password");
const statusMessage = document.querySelector("#status");
const approve = document.querySelector("#approve");
const deny = document.querySelector("#deny");
const signInControls = signInForm.querySelectorAll("input, button");
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

async function continueToConsent(verified) {
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
  signInForm.hidden = true;
  consent.hidden = false;
  approve.focus();
}

signInForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  signInControls.forEach((control) => { control.disabled = true; });
  statusMessage.textContent = "";
  try {
    const signIn = request("/token?grant_type=password", { email: email.value.trim(), password: password.value });
    password.value = "";
    await continueToConsent(await signIn);
  } catch {
    statusMessage.textContent = "Sign-in could not continue. Check your email and password or reconnect from Claude.";
  } finally {
    signInControls.forEach((control) => { control.disabled = false; });
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
signInForm.hidden = false;
