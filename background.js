// Closes the "auth successful" tabs that cosign, gitsign and chainctl leave
// open on localhost after their OAuth flows.

const AUTH_PATHS = new Map([
    ["Sigstore Authentication", "/auth/callback"],
    ["Chainguard Authentication", "/callback"],
]);
const AUTH_HOSTS = new Set(["localhost", "127.0.0.1"]);
const BADGE_COLOR = "#221F5C";

// onUpdated fires several times per page load (url, title, status), so
// remember which tabs are already being closed.
const closing = new Set();

// Count updates run one at a time so concurrent closes don't lose increments.
let countQueue = Promise.resolve();

// Firefox match patterns can't express ports, and the auth callback runs on a
// random loopback port, so the URL is checked here instead of in a filter.
function isAuthTab(tab) {
    if (!tab.url || !AUTH_PATHS.has(tab.title)) {
        return false;
    }
    try {
        const url = new URL(tab.url);
        return url.protocol === "http:" && AUTH_HOSTS.has(url.hostname)
            && url.pathname === AUTH_PATHS.get(tab.title);
    } catch {
        return false;
    }
}

async function showCount(count) {
    await browser.action.setBadgeText({ text: count ? String(count) : "" });
    await browser.action.setBadgeBackgroundColor({ color: BADGE_COLOR });
}

function incrementCount() {
    countQueue = countQueue
        .then(async () => {
            const { count = 0 } = await browser.storage.sync.get("count");
            const next = (parseInt(count, 10) || 0) + 1;
            await browser.storage.sync.set({ count: next });
            await showCount(next);
        })
        .catch((err) => console.error("failed to update closed-tab count:", err));
    return countQueue;
}

async function closeTab(tab) {
    if (closing.has(tab.id)) {
        return;
    }
    closing.add(tab.id);
    try {
        console.log(`closing auth tab ${tab.id}`);
        await browser.tabs.remove(tab.id);
        await incrementCount();
    } catch (err) {
        console.error(`failed to close tab ${tab.id}:`, err);
    } finally {
        closing.delete(tab.id);
    }
}

async function closeExistingAuthTabs() {
    const tabs = await browser.tabs.query({});
    await Promise.all(tabs.filter(isAuthTab).map(closeTab));
}

function restoreBadge() {
    countQueue = countQueue
        .then(async () => {
            const { count = 0 } = await browser.storage.sync.get("count");
            await showCount(parseInt(count, 10) || 0);
        })
        .catch((err) => console.error("failed to restore badge:", err));
    return countQueue;
}

browser.tabs.onUpdated.addListener(
    (tabId, changeInfo, tab) => {
        if (isAuthTab(tab)) {
            closeTab(tab);
        }
    },
    { properties: ["url", "title", "status"] }
);

async function onLaunch() {
    await restoreBadge();
    await closeExistingAuthTabs();
}

function launch() {
    onLaunch().catch((err) => console.error("failed to sweep tabs:", err));
}

browser.runtime.onStartup.addListener(launch);
browser.runtime.onInstalled.addListener(launch);
