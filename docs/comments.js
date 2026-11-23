"use strict";

(() => {
  const list = document.getElementById("comment-list");
  const status = document.getElementById("comments-status");
  const more = document.getElementById("more-comments");
  const localDate = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  const batch = 6;
  let limit = batch;
  let current = null;

  function row(comment) {
    const li = document.createElement("li");
    const avatar = document.createElement("span");
    avatar.className = "comment-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = comment.author.slice(0, 2).toUpperCase();
    const content = document.createElement("div");
    const meta = document.createElement("div");
    meta.className = "comment-meta";
    const name = document.createElement("bdi");
    name.textContent = comment.author;
    const date = document.createElement("time");
    date.dateTime = comment.created;
    date.textContent = localDate.format(new Date(comment.created));
    meta.append(name, date);
    const message = document.createElement("p");
    message.className = "comment-text";
    message.dir = "auto";
    message.textContent = comment.text;
    content.append(meta, message);
    li.append(avatar, content);
    return li;
  }

  function show() {
    list.replaceChildren(...current.slice(0, limit).map(row));
    list.hidden = !current.length;
    more.hidden = current.length <= limit;
    status.hidden = !!current.length;
    status.textContent = current.length ? "" : "No approved comments yet.";
  }

  async function refresh() {
    try {
      const response = await fetch("comments.json", { cache: "no-cache" });
      if (!response.ok) throw Error();
      const next = await response.json();
      if (
        !Array.isArray(next) ||
        !next.every(
          (c) =>
            c &&
            typeof c.text === "string" &&
            typeof c.author === "string" &&
            typeof c.created === "string" &&
            Number.isFinite(Date.parse(c.created)),
        )
      )
        throw Error();
      if (JSON.stringify(next) !== JSON.stringify(current)) {
        current = next;
        show();
      }
    } catch {
      current = null;
      list.replaceChildren();
      list.hidden = more.hidden = true;
      status.hidden = false;
      status.textContent =
        "Comments are temporarily unavailable. You can still open the GitHub thread.";
    }
  }
  more.addEventListener("click", () => {
    limit += batch;
    show();
  });
  refresh();
  setInterval(
    () => {
      if (!document.hidden) refresh();
    },
    5 * 60 * 1000,
  );
})();
