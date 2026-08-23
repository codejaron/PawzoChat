/*!
 * PawzoChat - Multi-platform LLM-powered chatbot
 * Copyright (C) 2026  iwyxdxl
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * PawzoChat browser notifications. The server filters foreground devices
 * before sending: every push event received here must remain user-visible,
 * which is required by browsers such as Safari. */

self.addEventListener("push", event => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (error) {
    console.error("Invalid PawzoChat push payload", error);
  }

  const title = payload.title || "PawzoChat";
  const body = payload.body || "收到一条新消息";
  const scope = self.registration.scope;
  event.waitUntil(self.registration.showNotification(title, {
    body,
    icon: new URL("static/logo.png", scope).href,
    badge: new URL("static/logo.png", scope).href,
    data: payload.data || {},
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const personaId = event.notification.data?.persona_id || "";
  const target = new URL(self.registration.scope);
  if (personaId) target.searchParams.set("notification_persona", personaId);

  event.waitUntil((async () => {
    const windows = await clients.matchAll({
      type: "window",
      includeUncontrolled: true,
    });
    const scopeUrl = new URL(self.registration.scope);
    const existing = windows.find(client => {
      const clientUrl = new URL(client.url);
      return clientUrl.origin === scopeUrl.origin
        && clientUrl.pathname.startsWith(scopeUrl.pathname);
    });
    if (existing) {
      if ("navigate" in existing) await existing.navigate(target.href);
      return existing.focus();
    }
    return clients.openWindow(target.href);
  })());
});
