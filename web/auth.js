"use strict";
// Only the non-exportable signing key is persisted. Session tokens and page
// tickets stay in memory and are never included in URLs or browser storage.
window.courseAuth = (() => {
  const encoder = new TextEncoder();
  let keysPromise;
  let publicJwk;
  let token = null;
  let clockOffset = 0;
  const b64 = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const json64 = value => b64(encoder.encode(JSON.stringify(value)));

  async function keys() {
    if (!keysPromise) keysPromise = (async () => {
      if (!window.crypto?.subtle) throw new Error("Для защищённого просмотра откройте приложение в обновлённом Telegram.");
      let database;
      try {
        database = await new Promise((resolve, reject) => {
          const request = indexedDB.open("course-device-key", 1);
          request.onupgradeneeded = () => request.result.createObjectStore("keys");
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
          request.onblocked = () => reject(new Error("Storage unavailable"));
        });
        const stored = await new Promise((resolve, reject) => {
          const request = database.transaction("keys").objectStore("keys").get("signing");
          request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
        });
        if (stored?.privateKey?.type === "private" && stored.privateKey.extractable === false && stored.privateKey.algorithm.namedCurve === "P-256") {
          database.close(); return stored;
        }
      } catch { /* Private browsing may disable persistence; keep a memory key. */ }
      const generated = await crypto.subtle.generateKey({name: "ECDSA", namedCurve: "P-256"}, false, ["sign", "verify"]);
      if (database) {
        try {
          await new Promise((resolve, reject) => {
            const transaction = database.transaction("keys", "readwrite");
            transaction.objectStore("keys").put(generated, "signing");
            transaction.oncomplete = resolve; transaction.onerror = () => reject(transaction.error);
          });
        } catch { /* The session can still use the key held in memory. */ }
        finally { database.close(); }
      }
      return generated;
    })();
    return keysPromise;
  }

  async function headers(path, method = "GET", authenticated = true) {
    const pair = await keys();
    if (!publicJwk) {
      const {kty, crv, x, y} = await crypto.subtle.exportKey("jwk", pair.publicKey);
      publicJwk = {kty, crv, x, y};
    }
    const url = new URL(path, window.location.origin);
    const payload = {jti: b64(crypto.getRandomValues(new Uint8Array(24))), htm: method,
      htu: url.origin + url.pathname, iat: Math.floor((Date.now() + clockOffset) / 1000)};
    const result = {};
    if (authenticated) {
      if (!token) throw new Error("Откройте приложение заново из бота.");
      payload.ath = b64(await crypto.subtle.digest("SHA-256", encoder.encode(token)));
      result.Authorization = `DPoP ${token}`;
    }
    const data = `${json64({typ: "dpop+jwt", alg: "ES256", jwk: publicJwk})}.${json64(payload)}`;
    const signature = await crypto.subtle.sign({name: "ECDSA", hash: "SHA-256"}, pair.privateKey, encoder.encode(data));
    result.DPoP = `${data}.${b64(signature)}`;
    return result;
  }

  async function open(initData) {
    const clock = await fetch("/api/session/clock", {cache: "no-store", credentials: "omit"});
    if (!clock.ok) throw new Error("Не удалось связаться с сервером.");
    clockOffset = (await clock.json()).server_time * 1000 - Date.now();
    const response = await fetch("/api/session", {method: "POST", cache: "no-store", credentials: "omit",
      headers: {...await headers("/api/session", "POST", false), "Content-Type": "application/json"},
      body: JSON.stringify({init_data: initData})});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(typeof data.detail === "string" ? data.detail : "Не удалось открыть сеанс.");
      error.status = response.status; throw error;
    }
    token = data.access_token;
    delete data.access_token;
    return data;
  }
  return {headers, open, hasSession: () => Boolean(token)};
})();
