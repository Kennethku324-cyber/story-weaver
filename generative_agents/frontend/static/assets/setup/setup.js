/* Story Weaver 角色 Setup — 四步精靈 */
(function () {
  "use strict";

  var MIN_CHARS = 4, MAX_CHARS = 10;

  var state = {
    step: 1,
    templates: [],      // /api/setup/templates
    rooms: [],          // /api/setup/housing
    selected: [],       // 揀選順序嘅 template_id
    charData: {},       // template_id -> {display_name, occupation, personality, home}
    relData: {},        // "from||to" -> {score, desc}
  };

  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ---------- 初始化 ---------- */
  function init() {
    Promise.all([
      fetch("/api/setup/templates").then(function (r) { return r.json(); }),
      fetch("/api/setup/housing").then(function (r) { return r.json(); }),
    ]).then(function (results) {
      state.templates = results[0].templates || [];
      state.rooms = results[1].rooms || [];
      renderGallery();
      bindNav();
      bindStep4();
      refresh();
    }).catch(function () {
      $("#sw-gallery").innerHTML = "<p class='sw-loading'>載入失敗，請重新整理頁面。</p>";
    });
  }

  /* ---------- 步驟一：畫廊 ---------- */
  function renderGallery() {
    var html = state.templates.map(function (t) {
      var disabled = !t.assets_complete;
      return (
        "<div class='sw-card" + (disabled ? " disabled" : "") + "' data-template='" + esc(t.template_id) + "'" +
        (disabled ? " title='素材不完整，唔揀得'" : "") + ">" +
        "<span class='sw-order'></span>" +
        "<img src='" + esc(t.portrait_url) + "' alt='" + esc(t.template_id) + "'>" +
        "<div class='sw-card-name'>" + esc(t.template_id) + "</div>" +
        "<div class='sw-card-innate'>" + esc(t.innate) + "</div>" +
        "</div>"
      );
    }).join("");
    $("#sw-gallery").innerHTML = html;
    $all(".sw-card").forEach(function (card) {
      card.addEventListener("click", function () {
        if (card.classList.contains("disabled")) return;
        var id = card.getAttribute("data-template");
        var idx = state.selected.indexOf(id);
        if (idx >= 0) {
          state.selected.splice(idx, 1);
        } else {
          if (state.selected.length >= MAX_CHARS) return;
          state.selected.push(id);
          ensureCharData(id);
        }
        refresh();
      });
    });
  }

  function ensureCharData(id) {
    if (state.charData[id]) return;
    var t = state.templates.find(function (x) { return x.template_id === id; });
    state.charData[id] = {
      display_name: t.template_id,
      occupation: t.learned_first_line || t.template_id,
      personality: t.innate || "",
      home: t.living_area.slice(),
    };
  }

  function templateOf(id) {
    return state.templates.find(function (x) { return x.template_id === id; });
  }

  /* ---------- 步驟二：角色卡 ---------- */
  function renderCharCards() {
    var wrap = $("#sw-char-cards");
    wrap.innerHTML = state.selected.map(function (id, i) {
      var t = templateOf(id);
      var d = state.charData[id];
      var options = state.rooms.map(function (r) {
        var sel = (r.address.join("|") === d.home.join("|")) ? " selected" : "";
        return "<option value='" + esc(r.address.join("|")) + "'" + sel + ">" + esc(r.label) + "</option>";
      }).join("");
      return (
        "<div class='sw-char-card' data-template='" + esc(id) + "'>" +
        "<div class='sw-char-head'>" +
        "<img src='" + esc(t.portrait_url) + "' alt=''>" +
        "<div><div class='sw-char-title'>角色 " + (i + 1) + "</div>" +
        "<div class='sw-char-template'>模板：" + esc(id) + "</div></div>" +
        "<button type='button' class='sw-btn-small' data-action='reset'>用返模板預設</button>" +
        "</div>" +
        "<div class='sw-field'><label>角色名<span class='sw-req'>*</span></label>" +
        "<input type='text' maxlength='20' data-char-field='display_name' data-field='characters[" + i + "].display_name' value='" + esc(d.display_name) + "'></div>" +
        "<div class='sw-field'><label>職業<span class='sw-req'>*</span></label>" +
        "<input type='text' maxlength='200' data-char-field='occupation' data-field='characters[" + i + "].occupation' value='" + esc(d.occupation) + "'></div>" +
        "<div class='sw-field'><label>性格<span class='sw-req'>*</span></label>" +
        "<input type='text' maxlength='200' data-char-field='personality' data-field='characters[" + i + "].personality' value='" + esc(d.personality) + "'></div>" +
        "<div class='sw-field'><label>住所</label>" +
        "<select data-char-field='home' data-field='characters[" + i + "].home'>" + options + "</select></div>" +
        "</div>"
      );
    }).join("");

    $all(".sw-char-card").forEach(function (card) {
      var id = card.getAttribute("data-template");
      card.querySelectorAll("[data-char-field]").forEach(function (input) {
        input.addEventListener("input", function () {
          var f = input.getAttribute("data-char-field");
          state.charData[id][f] = (f === "home") ? input.value.split("|") : input.value;
        });
      });
      card.querySelector("[data-action='reset']").addEventListener("click", function () {
        delete state.charData[id];
        ensureCharData(id);
        renderCharCards();
      });
    });
  }

  /* ---------- 步驟三：關係矩陣 ---------- */
  function relKey(a, b) { return a + "||" + b; }

  function renderMatrix() {
    var names = state.selected.map(function (id) { return state.charData[id].display_name || id; });
    var head = "<tr><th></th>" + names.map(function (n) { return "<th>" + esc(n) + "</th>"; }).join("") + "</tr>";
    var rows = names.map(function (from, i) {
      var cells = names.map(function (to, j) {
        if (i === j) return "<td class='diag'></td>";
        var key = relKey(from, to);
        var rel = state.relData[key] || { score: 0, desc: "" };
        state.relData[key] = rel;
        return (
          "<td><div class='sw-rel-label'>" + esc(from) + " 對 " + esc(to) + "</div>" +
          "<div class='sw-rel-score-row'>" +
          "<input type='range' min='-100' max='100' step='5' value='" + rel.score + "' data-rel-score='" + esc(key) + "'>" +
          "<span class='sw-rel-score' data-rel-score-label='" + esc(key) + "'>" + rel.score + "</span>" +
          "</div>" +
          "<div class='sw-scale-hint'><span>-100 仇視</span><span>0 陌生</span><span>+100 摯愛</span></div>" +
          "<input type='text' class='sw-rel-desc' maxlength='200' placeholder='關係描述（可留空）' value='" + esc(rel.desc) + "' data-rel-desc='" + esc(key) + "'>" +
          "</td>"
        );
      }).join("");
      return "<tr><th>" + esc(from) + "</th>" + cells + "</tr>";
    }).join("");
    $("#sw-matrix").innerHTML = "<table class='sw-matrix'>" + head + rows + "</table>";

    $all("[data-rel-score]").forEach(function (slider) {
      slider.addEventListener("input", function () {
        var key = slider.getAttribute("data-rel-score");
        state.relData[key].score = parseInt(slider.value, 10);
        var label = document.querySelector("[data-rel-score-label='" + key + "']");
        label.textContent = slider.value;
        label.className = "sw-rel-score" + (slider.value > 0 ? " pos" : slider.value < 0 ? " neg" : "");
      });
    });
    $all("[data-rel-desc]").forEach(function (input) {
      input.addEventListener("input", function () {
        state.relData[input.getAttribute("data-rel-desc")].desc = input.value;
      });
    });
  }

  /* ---------- 步驟四 ---------- */
  function bindStep4() {
    $("#sw-story-opening").addEventListener("input", function () {
      $("#sw-opening-count").textContent = this.value.length;
    });
  }

  function renderEstimate() {
    var n = state.selected.length;
    $("#sw-estimate").textContent = n
      ? "預估每回合約 " + (n * 7) + " 次 LLM 調用（" + n + " 個角色），請留意開支同推演時間。"
      : "";
  }

  /* ---------- 導航 ---------- */
  function bindNav() {
    $("#sw-next").addEventListener("click", function () { goStep(state.step + 1); });
    $("#sw-prev").addEventListener("click", function () { goStep(state.step - 1); });
    $("#sw-submit").addEventListener("click", submit);
  }

  function goStep(step) {
    if (step < 1 || step > 4) return;
    if (step === 2) renderCharCards();
    if (step === 3) renderMatrix();
    if (step === 4) renderEstimate();
    state.step = step;
    clearErrors();
    refresh();
  }

  function refresh() {
    // 步驟 tab
    $all(".sw-step-tab").forEach(function (tab) {
      var s = parseInt(tab.getAttribute("data-step"), 10);
      tab.classList.toggle("active", s === state.step);
      tab.classList.toggle("done", s < state.step);
    });
    [1, 2, 3, 4].forEach(function (s) {
      $("#sw-panel-" + s).style.display = (s === state.step) ? "" : "none";
    });
    // 畫廊狀態
    var count = state.selected.length;
    $("#sw-pick-count").textContent = "已揀 " + count + "/" + MIN_CHARS + "（最少 4 個，最多 10 個）";
    $("#sw-pick-bar").style.width = Math.min(100, count / MIN_CHARS * 100) + "%";
    $all(".sw-card").forEach(function (card) {
      var id = card.getAttribute("data-template");
      var idx = state.selected.indexOf(id);
      card.classList.toggle("selected", idx >= 0);
      card.querySelector(".sw-order").textContent = idx >= 0 ? (idx + 1) : "";
    });
    // 按鈕
    $("#sw-prev").style.display = state.step > 1 ? "" : "none";
    $("#sw-next").style.display = state.step < 4 ? "" : "none";
    $("#sw-submit").style.display = state.step === 4 ? "" : "none";
    $("#sw-next").disabled = (state.step === 1 && count < MIN_CHARS);
  }

  /* ---------- 錯誤顯示 ---------- */
  function clearErrors() {
    $("#sw-errors").style.display = "none";
    $("#sw-errors").innerHTML = "";
    $all(".sw-invalid").forEach(function (el) { el.classList.remove("sw-invalid"); });
    $all(".sw-field-error").forEach(function (el) { el.remove(); });
  }

  function showErrors(errors) {
    var box = $("#sw-errors");
    box.innerHTML = "<strong>有啲欄位要執一執：</strong><ul>" +
      errors.map(function (e) { return "<li>" + esc(e.message) + "</li>"; }).join("") + "</ul>";
    box.style.display = "";
    errors.forEach(function (e) {
      var input = document.querySelector("[data-field='" + e.field + "']");
      if (input) {
        input.classList.add("sw-invalid");
        var msg = document.createElement("div");
        msg.className = "sw-field-error";
        msg.textContent = e.message;
        input.parentNode.appendChild(msg);
      }
    });
    // 跳到第一個出錯步驟
    var first = errors[0] ? errors[0].field : "";
    var step = 4;
    if (first.indexOf("characters[") === 0) {
      step = /display_name|occupation|personality|home/.test(first) ? 2 : 2;
    } else if (first.indexOf("relationships") === 0) {
      step = 3;
    }
    if (step !== state.step) goStep(step);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /* ---------- 提交 ---------- */
  function gatherPayload() {
    var characters = state.selected.map(function (id) {
      var d = state.charData[id];
      return {
        template_id: id,
        display_name: (d.display_name || "").trim(),
        occupation: (d.occupation || "").trim(),
        personality: (d.personality || "").trim(),
        home: d.home,
      };
    });
    var relationships = [];
    var names = characters.map(function (c) { return c.display_name; });
    names.forEach(function (from) {
      names.forEach(function (to) {
        if (from === to) return;
        var rel = state.relData[relKey(from, to)];
        if (rel && (rel.score !== 0 || (rel.desc || "").trim())) {
          relationships.push({ from: from, to: to, score: rel.score, desc: (rel.desc || "").trim() });
        }
      });
    });
    return {
      story_name: $("#sw-story-name").value.trim(),
      story_opening: $("#sw-story-opening").value.trim(),
      characters: characters,
      relationships: relationships,
    };
  }

  function clientValidate(payload) {
    var errors = [];
    payload.characters.forEach(function (c, i) {
      if (!c.display_name) errors.push({ field: "characters[" + i + "].display_name", message: "角色名必填" });
      if (!c.occupation) errors.push({ field: "characters[" + i + "].occupation", message: "職業必填" });
      if (!c.personality) errors.push({ field: "characters[" + i + "].personality", message: "性格必填" });
    });
    if (!payload.story_name) errors.push({ field: "story_name", message: "故事名必填" });
    if (payload.story_opening.length < 10) errors.push({ field: "story_opening", message: "故事開端最少 10 字" });
    return errors;
  }

  function submit() {
    clearErrors();
    var payload = gatherPayload();
    var errors = clientValidate(payload);
    if (errors.length) { showErrors(errors); return; }

    // 未設定關係確認
    var n = payload.characters.length;
    var totalPairs = n * (n - 1);
    var unset = totalPairs - payload.relationships.length;
    if (unset > 0) {
      var ok = window.confirm("有 " + unset + " 對關係未設定，佢哋會以陌生人開局，繼續？");
      if (!ok) return;
    }

    $("#sw-progress-modal").style.display = "flex";
    $("#sw-progress-text").textContent = "正在為角色安頓住所……寫入記憶……";

    fetch("/api/setup/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (res) {
      return res.json().then(function (body) { return { status: res.status, body: body }; });
    }).then(function (result) {
      $("#sw-progress-modal").style.display = "none";
      if (result.status === 201) {
        var notes = [];
        if (result.body.clamped && result.body.clamped.length) {
          notes.push("有 " + result.body.clamped.length + " 個好感度數值超出範圍，已自動調整去 -100 ~ +100。");
        }
        if (result.body.filled_relationships) {
          notes.push("已補 " + result.body.filled_relationships + " 對陌生關係。");
        }
        if (notes.length) window.alert(notes.join("\n"));
        window.location.href = result.body.redirect;
      } else {
        var errs = result.body.errors || [{ field: "_", message: "未知錯誤" }];
        if (result.body.suggestion) {
          errs.push({ field: "story_name", message: "建議改用：" + result.body.suggestion });
        }
        showErrors(errs);
      }
    }).catch(function () {
      $("#sw-progress-modal").style.display = "none";
      showErrors([{ field: "_", message: "網絡錯誤，唔該再試一次" }]);
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
