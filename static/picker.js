/* Type-to-find switch picker, used wherever you choose switches.

   SwitchPicker.attach(select) puts a search box in front of a <select>. Click it to see every
   choice, or type part of a switch's name, address, building or location ("b3", "10.0.2",
   "idf c9300") and pick from what matches with the mouse, or the arrow keys and Enter.
   The <select> stays on the page (hidden) and keeps the value, so the page reads it and listens
   for "change" on it exactly as before.

   SwitchPicker.fillScope(select, switches, {types, value}) fills a select with "Every switch",
   one building, or one switch, the way the Port Analyzer, Port documentation and Network map
   pages offer them. */
(function () {
  "use strict";
  var MAX_SHOWN = 1000;
  var uid = 0;
  var native = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");

  function natural(a, b) { return String(a || "").localeCompare(String(b || ""), undefined, { numeric: true }); }

  // "B3-IDF (10.0.2.13)": what the box shows once a switch is picked
  function label(s) {
    var what = s.name || s.location;
    return what ? what + " (" + s.address + ")" : s.address;
  }

  function sortSwitches(list) {
    return list.slice().sort(function (a, b) {
      return natural(a.building, b.building) || natural(a.name || a.location, b.name || b.location)
        || natural(a.address, b.address);
    });
  }

  // One switch as an <option>: the name on top, the rest in grey under it, all of it searchable
  function option(s, value, typeName) {
    var o = document.createElement("option");
    var main = s.name || s.location || s.address;
    o.value = value;
    o.textContent = label(s);
    o.setAttribute("data-primary", main);
    o.setAttribute("data-secondary", [main !== s.address ? s.address : "", s.name ? s.location : "", s.building,
      typeName].filter(Boolean).join(" · "));
    o.setAttribute("data-search", [s.name, s.address, s.building, s.location, typeName].join(" ").toLowerCase());
    return o;
  }

  function fillScope(select, switches, opts) {
    opts = opts || {};
    var types = opts.types || {};
    var saved = switches.filter(function (s) { return s.address; });
    var nodes = [];
    var every = document.createElement("option");
    every.value = "";
    every.textContent = "Every switch (" + saved.length + ")";
    nodes.push(every);
    var counts = {};
    saved.forEach(function (s) { if (s.building) counts[s.building] = (counts[s.building] || 0) + 1; });
    var buildings = Object.keys(counts).sort(natural);
    if (buildings.length) {
      var g = document.createElement("optgroup");
      g.label = "One building";
      buildings.forEach(function (b) {
        var o = document.createElement("option");
        o.value = "b:" + b;
        o.textContent = b + " (" + counts[b] + ")";
        o.setAttribute("data-search", ("building " + b).toLowerCase());
        g.appendChild(o);
      });
      nodes.push(g);
    }
    if (saved.length) {
      var g2 = document.createElement("optgroup");
      g2.label = "One switch";
      sortSwitches(saved).forEach(function (s) { g2.appendChild(option(s, "s:" + s.address, types[s.type])); });
      nodes.push(g2);
    }
    select.textContent = "";
    nodes.forEach(function (n) { select.appendChild(n); });
    var want = opts.value || "";
    select.value = Array.prototype.some.call(select.options, function (o) { return o.value === want; }) ? want : "";
  }

  function attach(select, opts) {
    if (select._picker) return select._picker;
    opts = opts || {};
    var id = "picker-" + (++uid);
    var box = document.createElement("div");
    var input = document.createElement("input");
    var list = document.createElement("div");
    box.className = "picker";
    input.type = "text";
    input.className = "picker-input";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = opts.placeholder || "Type a name or address";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", id);
    list.className = "picker-list";
    list.id = id;
    list.setAttribute("role", "listbox");
    list.hidden = true;
    box.appendChild(input);
    box.appendChild(list);
    // In front of the select, so clicking the field's label focuses the box
    select.parentNode.insertBefore(box, select);
    select.hidden = true;
    select.tabIndex = -1;

    var open = false, typed = false, items = [], active = -1, selectOnUp = false;
    var picker = select._picker = { input: input, typed: "", refresh: function () { show(true); } };

    function current() {
      var o = select.options[select.selectedIndex];
      return o ? o.textContent : "";
    }
    function show(force) {
      input.disabled = select.disabled;
      if (force || !open) input.value = current();
    }
    // Pages set select.value in their own code; keep the box showing it
    Object.defineProperty(select, "value", {
      configurable: true,
      get: function () { return native.get.call(select); },
      set: function (v) { native.set.call(select, v); show(); }
    });

    function entries() {
      var out = [];
      Array.prototype.forEach.call(select.children, function (child) {
        if (child.tagName === "OPTGROUP") {
          Array.prototype.forEach.call(child.children, function (o) { out.push({ opt: o, group: child.label }); });
        } else if (child.tagName === "OPTION") {
          out.push({ opt: child, group: "" });
        }
      });
      return out;
    }
    function highlight(el, text, tokens) {
      var lower = text.toLowerCase(), at = -1, len = 0;
      tokens.some(function (t) { at = lower.indexOf(t); len = t.length; return at >= 0; });
      if (at < 0) { el.textContent = text; return; }
      var b = document.createElement("b");
      b.textContent = text.slice(at, at + len);
      el.appendChild(document.createTextNode(text.slice(0, at)));
      el.appendChild(b);
      el.appendChild(document.createTextNode(text.slice(at + len)));
    }
    function setActive(i) {
      items.forEach(function (it, n) { it.row.classList.toggle("active", n === i); });
      active = i;
      if (i >= 0 && items[i]) {
        input.setAttribute("aria-activedescendant", items[i].row.id);
        var row = items[i].row;
        if (row.offsetTop < list.scrollTop) list.scrollTop = row.offsetTop - 4;
        else if (row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight) {
          list.scrollTop = row.offsetTop + row.offsetHeight - list.clientHeight + 4;
        }
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }
    function render() {
      var q = typed ? input.value.trim().toLowerCase() : "";
      var tokens = q ? q.split(/\s+/) : [];
      var lastGroup = null, more = 0, chosenAt = 0;
      list.textContent = "";
      items = [];
      entries().forEach(function (e) {
        var o = e.opt;
        if (o.disabled) return;
        var hay = (o.getAttribute("data-search") || "") + " " + o.textContent.toLowerCase();
        if (!o.hasAttribute("data-always") && tokens.some(function (t) { return hay.indexOf(t) < 0; })) return;
        if (items.length >= MAX_SHOWN) { more++; return; }
        if (e.group && e.group !== lastGroup) {
          var head = document.createElement("div");
          head.className = "picker-group";
          head.textContent = e.group;
          head.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
          list.appendChild(head);
        }
        lastGroup = e.group;
        var row = document.createElement("div");
        row.className = "picker-opt";
        row.id = id + "-" + items.length;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(o.selected));
        var main = document.createElement("span");
        main.className = "picker-main";
        highlight(main, o.getAttribute("data-primary") || o.textContent, tokens);
        row.appendChild(main);
        var sub = o.getAttribute("data-secondary");
        if (sub) {
          var small = document.createElement("span");
          small.className = "picker-sub";
          highlight(small, sub, tokens);
          row.appendChild(small);
        }
        var n = items.length;
        row.addEventListener("mousedown", function (ev) { ev.preventDefault(); choose(o); });
        row.addEventListener("mousemove", function () { if (active !== n) setActive(n); });
        if (o.selected) chosenAt = n;
        items.push({ row: row, opt: o });
        list.appendChild(row);
      });
      if (!items.some(function (it) { return !it.opt.hasAttribute("data-always"); }) && tokens.length) {
        var none = document.createElement("div");
        none.className = "picker-note";
        none.textContent = "Nothing on your list matches “" + input.value.trim() + "”. Try part of the name or the address.";
        list.appendChild(none);
      }
      if (more) {
        var note = document.createElement("div");
        note.className = "picker-note";
        note.textContent = more + " more: keep typing to narrow it down.";
        list.appendChild(note);
      }
      setActive(items.length ? (typed ? 0 : chosenAt) : -1);
    }
    function openList() {
      if (open || input.disabled) return;
      open = true;
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      render();
    }
    function close() {
      open = false;
      typed = false;
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      show(true);
    }
    function choose(o) {
      picker.typed = typed ? input.value.trim() : "";      // what was typed, for a page that wants it
      var changed = native.get.call(select) !== o.value;
      native.set.call(select, o.value);
      close();
      if (document.activeElement === input) input.select();   // typing again starts a new search
      if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    list.addEventListener("mousedown", function (e) { e.preventDefault(); });   // keep the box focused (scrollbar too)
    // Clicking in shows the whole list with the text selected, so typing starts a new search
    input.addEventListener("focus", function () { openList(); input.select(); selectOnUp = true; });
    input.addEventListener("mousedown", function () {
      if (document.activeElement === input && !open) { openList(); selectOnUp = true; }
    });
    input.addEventListener("mouseup", function (e) {
      if (selectOnUp) { e.preventDefault(); input.select(); selectOnUp = false; }
    });
    input.addEventListener("blur", close);
    input.addEventListener("input", function () {
      typed = true;
      if (!open) openList(); else render();
    });
    input.addEventListener("keydown", function (e) {
      selectOnUp = false;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!open) { openList(); return; }
        if (!items.length) return;
        var step = e.key === "ArrowDown" ? 1 : -1;
        setActive((active + step + items.length) % items.length);
      } else if (e.key === "Enter") {
        if (open && active >= 0) { e.preventDefault(); choose(items[active].opt); }
        else if (!open) { e.preventDefault(); openList(); }
      } else if (e.key === "Escape") {
        if (open) { e.preventDefault(); close(); input.select(); }
      } else if (e.key === "Tab") {
        if (open && typed && active >= 0) choose(items[active].opt);
      }
    });
    new MutationObserver(function () { show(); if (open) render(); })
      .observe(select, { childList: true, subtree: true, characterData: true, attributes: true,
                         attributeFilter: ["disabled"] });
    show(true);
    return picker;
  }

  window.SwitchPicker = { attach: attach, fillScope: fillScope, option: option, label: label,
                          sortSwitches: sortSwitches };
})();
