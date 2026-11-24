/* Deleting what the tools saved: one at a time (a Delete button on each), or all of one kind
   (a "Delete all" button that asks first). Used by every page that lists saved runs.

   Saved.bar(el, {kind, total, shown, noun: ["report", "reports"], onChange})
       fills `el` with "12 saved (showing the newest 10)" and a Delete all button.
   Saved.deleteButton({kind, name, label, onChange}) -> a Delete button for one saved run.
   Saved.confirm({title, body, yes}) -> Promise of true/false, from a dialog in the page. */
(function () {
  "use strict";

  function post(url, body) {
    return fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) { return { ok: r.ok, data: data }; });
    }).catch(function () { return { ok: false, data: { problems: ["Couldn't reach the app. Is it still running?"] } }; });
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // A dialog in the page: title, a line of text, the button that does it, and Cancel (which has the focus,
  // so Enter or Esc never deletes anything by accident). Without yes it's just a message with OK.
  function confirm(opts) {
    return new Promise(function (resolve) {
      var d = el("dialog", "confirm");
      d.setAttribute("aria-labelledby", "confirmTitle");
      var h = el("h2", null, opts.title);
      h.id = "confirmTitle";
      d.appendChild(h);
      if (opts.body) d.appendChild(el("p", null, opts.body));
      var row = el("div", "confirm-actions");
      var done = function (answer) { d.close(); d.remove(); resolve(answer); };
      var cancel = el("button", "btn", opts.yes ? "Cancel" : "OK");
      cancel.type = "button";
      cancel.addEventListener("click", function () { done(false); });
      if (opts.yes) {
        var yes = el("button", "btn danger", opts.yes);
        yes.type = "button";
        yes.addEventListener("click", function () { done(true); });
        row.appendChild(yes);
      }
      row.appendChild(cancel);
      d.appendChild(row);
      d.addEventListener("cancel", function (e) { e.preventDefault(); done(false); });   // Esc
      document.body.appendChild(d);
      d.showModal();
      cancel.focus();
    });
  }

  function problemOf(r) {
    return (r.data.problems && r.data.problems.join(" ")) || "Something went wrong.";
  }

  function deleteButton(opts) {
    var b = el("button", "btn small quiet del", "Delete");
    b.type = "button";
    b.setAttribute("aria-label", "Delete " + opts.label);
    b.addEventListener("click", function () {
      b.disabled = true;
      b.textContent = "Deleting…";
      post("/api/saved/delete", { kind: opts.kind, name: opts.name }).then(function (r) {
        if (r.ok || /already gone/.test(problemOf(r))) {      // gone either way: the list is redrawn
          if (opts.onChange) opts.onChange(opts.name);
          return;
        }
        b.disabled = false;
        b.textContent = "Delete";
        confirm({ title: "Couldn't delete it", body: problemOf(r) });
      });
    });
    return b;
  }

  function bar(container, opts) {
    container.textContent = "";
    container.hidden = !opts.total;
    if (!opts.total) return;
    var noun = opts.noun;
    var text = opts.total + " saved" + (opts.shown < opts.total ? " (showing the newest " + opts.shown + ")" : "");
    container.appendChild(el("span", "hint", text));
    var all = el("button", "btn small danger-outline", "Delete all");
    all.type = "button";
    all.addEventListener("click", function () {
      confirm({
        title: "Delete all " + opts.total + " saved " + (opts.total === 1 ? noun[0] : noun[1]) + "?",
        body: "Are you sure? " + (opts.total === 1 ? "It" : "They") + " will be deleted from your data folder for " +
          "good, including any not shown here. This can't be undone.",
        yes: "Yes, delete all " + opts.total
      }).then(function (sure) {
        if (!sure) return;
        all.disabled = true;
        all.textContent = "Deleting…";
        post("/api/saved/delete-all", { kind: opts.kind, confirm: true }).then(function (r) {
          all.disabled = false;
          all.textContent = "Delete all";
          if (opts.onChange) opts.onChange(null);
          if (!r.ok) confirm({ title: "Some couldn't be deleted", body: problemOf(r) });
        });
      });
    });
    container.appendChild(all);
  }

  window.Saved = { bar: bar, deleteButton: deleteButton, confirm: confirm };
})();
