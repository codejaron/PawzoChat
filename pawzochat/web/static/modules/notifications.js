/*!
 * PawzoChat - Multi-platform LLM-powered chatbot
 * Copyright (C) 2026  iwyxdxl
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published
 * by the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 *
 * PawzoChat browser-notification lifecycle. The browser subscription is the
 * source of truth for this device's switch; the server owns delivery policy,
 * global privacy, presence filtering, and invalid-endpoint cleanup.
 */

import { api } from "./api.js";
import { $, content, state } from "./state.js";
import { iconHtml } from "./utils.js";
import { hideLoading, showLoading, toast } from "./ui.js";
import {
  registerPageRenderer, setTopBar, switchTab,
} from "./navigation.js";
import { openChat } from "./chat.js";

const BASE = window.PAWZOCHAT_BASE || "";
const DEVICE_ID_KEY = "pawzo_notification_device_id";
const SUBSCRIBED_KEY = "pawzo_notification_registered";
const PRESENCE_INTERVAL_MS = 30000;

let _registrationPromise = null;
let _subscribed = false;
let _presenceTimer = null;

function _deviceId() {
  const existing = localStorage.getItem(DEVICE_ID_KEY) || "";
  if (/^[A-Za-z0-9_-]{16,128}$/.test(existing)) return existing;
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  const generated = Array.from(bytes, byte =>
    byte.toString(16).padStart(2, "0")
  ).join("");
  localStorage.setItem(DEVICE_ID_KEY, generated);
  return generated;
}

function _isIOS() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function _isStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches
    || navigator.standalone === true;
}

function _capability() {
  if (!window.isSecureContext) {
    return {
      supported: false,
      reason: "需要通过受信任的 HTTPS 地址访问；仅本机 localhost 可使用 HTTP",
    };
  }
  if (_isIOS() && !_isStandalone()) {
    return {
      supported: false,
      reason: "iPhone/iPad 需先用 Safari 添加到主屏幕，再从主屏幕打开",
    };
  }
  if (!("serviceWorker" in navigator)
      || !("PushManager" in window)
      || !("Notification" in window)) {
    return { supported: false, reason: "当前浏览器不支持通知" };
  }
  if (Notification.permission === "denied") {
    return {
      supported: false,
      reason: "通知权限已被浏览器关闭，请在该网站的浏览器设置中重新允许",
    };
  }
  return { supported: true, reason: "" };
}

function _ensureServiceWorker() {
  if (!_registrationPromise) {
    _registrationPromise = navigator.serviceWorker.register(
      `${BASE}/service-worker.js`,
      { scope: `${BASE}/` },
    );
  }
  return _registrationPromise;
}

function _applicationServerKey(value) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  const raw = atob((value + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, char => char.charCodeAt(0));
}

async function _registerOnServer(registration, subscription) {
  const json = subscription.toJSON();
  const res = await api.post("/api/notifications/subscriptions", {
    device_id: _deviceId(),
    subscription: {
      endpoint: json.endpoint,
      keys: json.keys,
    },
    expiration_time: subscription.expirationTime ?? json.expirationTime ?? null,
    scope: registration.scope,
  });
  if (res.status >= 400) {
    throw new Error(res.data?.error || "服务端保存订阅失败");
  }
}

async function _removeOnServer(endpoint = "") {
  const res = await api.post("/api/notifications/unsubscribe", {
    device_id: _deviceId(),
    endpoint,
  });
  if (res.status >= 400) {
    throw new Error(res.data?.error || "服务端删除订阅失败");
  }
}

function _sendPresence(foreground) {
  if (!_subscribed) return;
  const payload = new Blob([JSON.stringify({
    device_id: _deviceId(),
    foreground,
  })], { type: "application/json" });
  const queued = navigator.sendBeacon(
    `${BASE}/api/notifications/presence`,
    payload,
  );
  if (!queued) console.error("PawzoChat notification presence was not queued");
}

function _isForeground() {
  return document.visibilityState === "visible" && document.hasFocus();
}

function _startPresence() {
  if (_presenceTimer) clearInterval(_presenceTimer);
  _sendPresence(_isForeground());
  _presenceTimer = setInterval(() => {
    if (_subscribed) _sendPresence(_isForeground());
  }, PRESENCE_INTERVAL_MS);
}

async function _syncExistingSubscription() {
  const capability = _capability();
  const wasRegistered = localStorage.getItem(SUBSCRIBED_KEY) === "1";
  if (!capability.supported) {
    // Revoking a site's permission may remove the browser subscription before
    // the Push service returns 410. If this origin had registered before, clean
    // the server record immediately instead of waiting for a future message.
    if ("Notification" in window && Notification.permission === "denied" && wasRegistered
        && "serviceWorker" in navigator) {
      let endpoint = "";
      try {
        const registration = await _ensureServiceWorker();
        const subscription = await registration.pushManager.getSubscription();
        endpoint = subscription?.endpoint || "";
        if (subscription) await subscription.unsubscribe();
      } finally {
        await _removeOnServer(endpoint);
        localStorage.removeItem(SUBSCRIBED_KEY);
        _subscribed = false;
      }
    }
    return;
  }
  const registration = await _ensureServiceWorker();
  const subscription = await registration.pushManager.getSubscription();
  if (Notification.permission === "granted" && subscription) {
    await _registerOnServer(registration, subscription);
    localStorage.setItem(SUBSCRIBED_KEY, "1");
    _subscribed = true;
    _startPresence();
  } else if (wasRegistered) {
    await _removeOnServer();
    localStorage.removeItem(SUBSCRIBED_KEY);
    _subscribed = false;
  }
}

function _permissionLabel(subscribed, capability) {
  if (!capability.supported) return capability.reason;
  if (subscribed) return "已在当前设备开启；使用本设备聊天时不会重复弹出通知";
  if (Notification.permission === "granted") return "当前设备尚未订阅";
  return "开启时浏览器会请求一次通知权限";
}

export async function renderNotificationSettings() {
  setTopBar("通知", true, "");
  content().innerHTML = `<div class="loading-center"><div class="spinner"></div></div>`;

  if (!state.settings) {
    try {
      state.settings = await api.get("/api/settings", { bypassCache: true });
    } catch (error) {
      toast("加载通知设置失败", "error");
    }
  }

  const capability = _capability();
  let subscribed = false;
  if (capability.supported) {
    try {
      const registration = await _ensureServiceWorker();
      const subscription = await registration.pushManager.getSubscription();
      subscribed = Notification.permission === "granted" && !!subscription;
    } catch (error) {
      console.error("Failed to inspect notification subscription", error);
    }
  }
  _subscribed = subscribed;

  const hideContent = !!state.settings?.notifications?.hide_content;
  content().innerHTML = `<div class="page">
    <div class="card">
      <div class="form-group"><div class="form-row">
        <label>当前设备通知</label>
        <label class="switch-wrap"><input type="checkbox" id="notification-device-switch"
          ${subscribed ? "checked" : ""} ${capability.supported ? "" : "disabled"}
          onchange="PawzoChat.toggleDeviceNotifications()"><span class="switch-track"></span></label>
      </div></div>
      <div class="form-hint">${_permissionLabel(subscribed, capability)}</div>
    </div>
    <div class="card" style="margin-top:12px">
      <div class="form-group"><div class="form-row">
        <label>隐藏通知内容</label>
        <label class="switch-wrap"><input type="checkbox" id="notification-hide-content"
          ${hideContent ? "checked" : ""}
          onchange="PawzoChat.toggleNotificationContentPrivacy()"><span class="switch-track"></span></label>
      </div></div>
      <div class="form-hint">开启后，所有设备只显示“发来一条新消息”</div>
    </div>
    <div class="card" style="margin-top:12px">
      <div style="padding:14px 16px;font-size:13px;color:var(--text-2);line-height:1.7">
        <div style="display:flex;gap:8px;align-items:flex-start">
          <span style="color:var(--primary);font-size:18px">${iconHtml("ri-information-line")}</span>
          <span>每条助手回复会单独通知。当前正在使用的设备不弹，其他后台设备照常接收。单个对话可在聊天右上角“更多操作”中开启免打扰。</span>
        </div>
        <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--divider)">
          <div style="font-weight:600;color:var(--text-1);margin-bottom:4px">支持的平台</div>
          <div>• Windows：Chrome、Edge、Firefox 可直接开启</div>
          <div>• macOS：Safari 16.1+、Chrome、Edge、Firefox 可直接开启</div>
          <div>• Android：Chrome、Edge、Firefox 等可直接开启，不要求添加到主屏幕</div>
          <div>• iPhone/iPad：需要 iOS/iPadOS 16.4+，先在 Safari 中添加到主屏幕，再从主屏幕图标打开并开启</div>
          <div style="margin-top:6px">以上设备远程访问时都必须使用浏览器信任的 HTTPS 地址；页面仍会按当前浏览器实际能力检测。</div>
        </div>
      </div>
    </div>
  </div>`;
}

export async function toggleDeviceNotifications() {
  const input = $("notification-device-switch");
  if (!input) return;
  const enabling = input.checked;
  input.disabled = true;
  showLoading(enabling ? "正在开启通知…" : "正在关闭通知…");
  try {
    if (enabling) {
      const capability = _capability();
      if (!capability.supported) throw new Error(capability.reason);
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        throw new Error(permission === "denied"
          ? "通知权限被拒绝，请在浏览器的网站设置中重新允许"
          : "未获得通知权限");
      }

      const registration = await _ensureServiceWorker();
      let subscription = await registration.pushManager.getSubscription();
      let created = false;
      if (!subscription) {
        const keyResult = await api.get(
          "/api/notifications/vapid-public-key",
          { bypassCache: true },
        );
        if (!keyResult.public_key) {
          throw new Error(keyResult.error || "无法获取通知公钥");
        }
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: _applicationServerKey(keyResult.public_key),
        });
        created = true;
      }
      try {
        await _registerOnServer(registration, subscription);
      } catch (error) {
        if (created) await subscription.unsubscribe();
        throw error;
      }
      localStorage.setItem(SUBSCRIBED_KEY, "1");
      _subscribed = true;
      _startPresence();
      toast("当前设备通知已开启", "success");
    } else {
      const registration = await _ensureServiceWorker();
      const subscription = await registration.pushManager.getSubscription();
      const endpoint = subscription?.endpoint || "";
      if (subscription) {
        const unsubscribed = await subscription.unsubscribe();
        if (!unsubscribed) throw new Error("浏览器未能取消通知订阅");
      }
      await _removeOnServer(endpoint);
      localStorage.removeItem(SUBSCRIBED_KEY);
      _subscribed = false;
      _sendPresence(false);
      toast("当前设备通知已关闭", "success");
    }
  } catch (error) {
    input.checked = !enabling;
    toast(error.message || "通知设置失败", "error");
  } finally {
    hideLoading();
    await renderNotificationSettings();
  }
}

export async function toggleNotificationContentPrivacy() {
  const input = $("notification-hide-content");
  if (!input) return;
  const hideContent = input.checked;
  input.disabled = true;
  try {
    const res = await api.patch("/api/settings", {
      notifications: { hide_content: hideContent },
    });
    if (res.status >= 400) {
      throw new Error(res.data?.error || "保存失败");
    }
    if (!res.data?.notifications) {
      throw new Error("服务端未返回通知设置");
    }
    state.settings = state.settings || {};
    state.settings.notifications = res.data.notifications;
    toast(hideContent ? "通知内容已隐藏" : "通知内容将正常显示", "success");
  } catch (error) {
    input.checked = !hideContent;
    toast(error.message || "保存失败", "error");
  } finally {
    input.disabled = false;
  }
}

async function _openNotificationTarget() {
  const url = new URL(window.location.href);
  const personaId = url.searchParams.get("notification_persona") || "";
  if (!personaId) return;
  url.searchParams.delete("notification_persona");
  history.replaceState({}, "", url.href);
  switchTab("chat");

  for (let attempt = 0; attempt < 20; attempt++) {
    if (state.conversations.some(item => item.persona_id === personaId)) {
      openChat(personaId);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  toast("通知对应的对话不存在", "error");
}

export function initNotifications() {
  document.addEventListener("visibilitychange", () => {
    _sendPresence(_isForeground());
  });
  window.addEventListener("focus", () => _sendPresence(_isForeground()));
  window.addEventListener("blur", () => _sendPresence(false));
  window.addEventListener("pagehide", () => _sendPresence(false));

  _syncExistingSubscription().catch(error => {
    console.error("Failed to synchronize PawzoChat notification subscription", error);
  });
  _openNotificationTarget().catch(error => {
    console.error("Failed to open notification target", error);
  });
}

registerPageRenderer("settingsNotifications", renderNotificationSettings);
