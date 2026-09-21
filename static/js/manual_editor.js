(() => {
  "use strict";

  const cfg = window.SAEDAM_MANUAL_EDITOR;
  if (!cfg) return;

  const page = document.getElementById("manualEditorPage");
  const tocList = document.getElementById("editorTocList");
  const sectionContainer = document.getElementById("sectionContainer");
  const titleInput = document.getElementById("manualTitle");
  const descInput = document.getElementById("manualDescription");
  const saveBtn = document.getElementById("saveBtn");
  const previewBtn = document.getElementById("previewBtn");
  const publishBtn = document.getElementById("publishBtn");
  const txtInput = document.getElementById("txtImportInput");
  const saveState = document.getElementById("saveState");
  const toast = document.getElementById("manualToast");


  let dirty = false;
  let saving = false;
  let sectionSeed = Date.now();
  let draggedKey = null;
  let sectionObserver = null;

  function showToast(message, isError=false){
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.classList.add("show");
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => toast.classList.remove("show"), 2400);
  }

  function markDirty(){
    dirty = true;
    saveState.textContent = "수정중";
  }

  function sectionEls(){
    return [...sectionContainer.querySelectorAll(".manual-edit-section")];
  }

  function tocEls(){
    return [...tocList.querySelectorAll(".manual-editor-toc-item")];
  }

  function escapeHtml(value){
    const div = document.createElement("div");
    div.textContent = value ?? "";
    return div.innerHTML;
  }

  function renumber(){
    sectionEls().forEach((section, idx) => {
      section.querySelector(".section-number").textContent = idx + 1;
      section.id = `manual-editor-section-${idx + 1}`;
    });
    tocEls().forEach((item, idx) => {
      item.querySelector(".num").textContent = idx + 1;
    });
  }

  function syncTocTitles(){
    sectionEls().forEach(section => {
      const key = section.dataset.sectionKey;
      const title = section.querySelector(".section-title-input").value.trim() || "제목 없음";
      const toc = tocList.querySelector(`[data-section-key="${CSS.escape(key)}"]`);
      if (toc) toc.querySelector(".toc-text").textContent = title;
    });
  }

  function scrollToSection(key){
    const el = sectionContainer.querySelector(`[data-section-key="${CSS.escape(key)}"]`);
    if (el) el.scrollIntoView({behavior:"smooth", block:"start"});
  }

  function createTocItem(key, title){
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "manual-editor-toc-item";
    btn.draggable = true;
    btn.dataset.sectionKey = key;
    btn.innerHTML = `
      <span class="num"></span>
      <span class="toc-text">${escapeHtml(title || "새 목차")}</span>
      <span class="drag">⋮⋮</span>
    `;
    tocList.appendChild(btn);
    bindTocItem(btn);
  }

  function editorToolbarHtml(){
    return `
      <div class="saedam-editor-toolbar" role="toolbar" aria-label="본문 편집 도구">
        <button type="button" data-format="h3">소제목</button>
        <button type="button" data-command="bold" title="굵게"><b>B</b></button>
        <button type="button" data-command="italic" title="기울임"><i>I</i></button>
        <button type="button" data-command="underline" title="밑줄"><u>U</u></button>

        <select class="manual-font-size-select" title="글자 크기">
          <option value="">글자크기</option>
          <option value="12px">12</option>
          <option value="14px">14</option>
          <option value="16px">16</option>
          <option value="18px">18</option>
          <option value="20px">20</option>
          <option value="24px">24</option>
          <option value="28px">28</option>
          <option value="32px">32</option>
        </select>
        <label class="manual-color-control" title="글자색">
          <span>글자색</span>
          <input class="manual-font-color-input" type="color" value="#182a2c">
        </label>

        <span class="toolbar-divider"></span>
        <button type="button" data-command="insertUnorderedList">• 목록</button>
        <button type="button" data-command="insertOrderedList">1. 목록</button>
        <button type="button" data-action="link">링크</button>

        <span class="toolbar-divider"></span>
        <button type="button" data-action="table">표 삽입</button>
        <button type="button" data-action="table-row-add" title="선택 셀 아래 행 추가">행＋</button>
        <button type="button" data-action="table-row-delete" title="선택된 행 삭제">행−</button>
        <button type="button" data-action="table-col-add" title="선택 셀 오른쪽 열 추가">열＋</button>
        <button type="button" data-action="table-col-delete" title="선택된 열 삭제">열−</button>
        <button type="button" data-action="table-cell-size" title="선택 셀의 폭과 높이 조절">셀 크기</button>

        <span class="toolbar-divider"></span>
        <button type="button" data-action="note">안내</button>
        <button type="button" data-action="warn">주의</button>
        <button type="button" data-action="danger">경고</button>
        <button type="button" data-action="image">이미지</button>
        <input class="section-image-input" type="file" accept="image/*" hidden>
      </div>
    `;
  }

  function createSection(data={}){
    const key = `section-new-${sectionSeed++}`;
    const section = document.createElement("section");
    section.className = "manual-edit-section";
    section.dataset.sectionKey = key;
    section.innerHTML = `
      <div class="manual-edit-section-head">
        <div class="section-number"></div>
        <div class="section-head-fields">
          <input class="section-title-input" type="text" maxlength="200"
                 value="${escapeHtml(data.title || "새 목차")}" placeholder="목차 제목">
          <input class="section-desc-input" type="text" maxlength="1000"
                 value="${escapeHtml(data.description || "")}" placeholder="이 목차에 대한 간단한 설명 (선택)">
        </div>
        <button class="section-delete-btn" type="button" title="목차 삭제">삭제</button>
      </div>
      ${editorToolbarHtml()}
      <div class="saedam-rich-editor" contenteditable="true"
           data-placeholder="이곳에 메뉴얼 내용을 작성하세요.">${data.content_html || ""}</div>
    `;
    sectionContainer.appendChild(section);
    createTocItem(key, data.title || "새 목차");
    bindSection(section);
    decorateImages(section);
    if (sectionObserver) sectionObserver.observe(section);
    renumber();
    markDirty();
    setTimeout(() => section.scrollIntoView({behavior:"smooth", block:"start"}), 20);
    return section;
  }

  function removeSection(section){
    if (sectionEls().length <= 1){
      showToast("목차는 한 개 이상 필요합니다.", true);
      return;
    }
    if (!confirm("이 목차와 작성 내용을 삭제할까요?")) return;

    const key = section.dataset.sectionKey;
    section.remove();
    tocList.querySelector(`[data-section-key="${CSS.escape(key)}"]`)?.remove();
    renumber();
    markDirty();
  }

  function bindTocItem(item){
    item.addEventListener("click", () => scrollToSection(item.dataset.sectionKey));

    item.addEventListener("dragstart", e => {
      draggedKey = item.dataset.sectionKey;
      item.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });

    item.addEventListener("dragend", () => {
      item.classList.remove("dragging");
      draggedKey = null;
    });

    item.addEventListener("dragover", e => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    });

    item.addEventListener("drop", e => {
      e.preventDefault();
      if (!draggedKey || draggedKey === item.dataset.sectionKey) return;

      const draggedToc = tocList.querySelector(`[data-section-key="${CSS.escape(draggedKey)}"]`);
      const draggedSection = sectionContainer.querySelector(`[data-section-key="${CSS.escape(draggedKey)}"]`);
      const targetSection = sectionContainer.querySelector(`[data-section-key="${CSS.escape(item.dataset.sectionKey)}"]`);
      if (!draggedToc || !draggedSection || !targetSection) return;

      const all = tocEls();
      const from = all.indexOf(draggedToc);
      const to = all.indexOf(item);

      if (from < to){
        item.after(draggedToc);
        targetSection.after(draggedSection);
      } else {
        item.before(draggedToc);
        targetSection.before(draggedSection);
      }
      renumber();
      markDirty();
    });
  }

  function restoreSelection(editor){
    const range = editor._savedRange;
    if (!range) return false;
    try{
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      return true;
    }catch(_){
      return false;
    }
  }

  function rememberSelection(editor){
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    if (editor.contains(range.commonAncestorContainer)){
      editor._savedRange = range.cloneRange();

      const node = range.startContainer.nodeType === Node.ELEMENT_NODE
        ? range.startContainer
        : range.startContainer.parentElement;
      const cell = node?.closest?.("td,th");
      if (cell && editor.contains(cell)){
        setActiveCell(editor, cell);
      }
    }
  }

  function insertHtml(editor, html){
    editor.focus();
    restoreSelection(editor);
    document.execCommand("insertHTML", false, html);
    rememberSelection(editor);
    markDirty();
  }

  function applyInlineStyle(editor, property, value){
    editor.focus();
    restoreSelection(editor);

    const sel = window.getSelection();
    if (!sel.rangeCount || sel.isCollapsed){
      showToast("서식을 적용할 글자를 먼저 선택해 주세요.", true);
      return;
    }

    const range = sel.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)){
      showToast("본문 안의 글자를 선택해 주세요.", true);
      return;
    }

    const span = document.createElement("span");
    span.style[property] = value;

    try{
      const fragment = range.extractContents();
      span.appendChild(fragment);
      range.insertNode(span);

      const newRange = document.createRange();
      newRange.selectNodeContents(span);
      sel.removeAllRanges();
      sel.addRange(newRange);
      editor._savedRange = newRange.cloneRange();
      markDirty();
    }catch(err){
      console.error(err);
      showToast("선택 영역에 서식을 적용하지 못했습니다. 한 문단 안에서 다시 선택해 주세요.", true);
    }
  }

  function setActiveCell(editor, cell){
    editor.querySelectorAll(".manual-cell-selected").forEach(el => el.classList.remove("manual-cell-selected"));
    if (cell){
      cell.classList.add("manual-cell-selected");
      editor._activeCell = cell;
    }else{
      editor._activeCell = null;
    }
  }

  function getActiveCell(editor){
    const cell = editor._activeCell;
    if (cell && editor.contains(cell)) return cell;

    const range = editor._savedRange;
    if (!range) return null;
    const node = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer
      : range.startContainer.parentElement;
    const found = node?.closest?.("td,th");
    return found && editor.contains(found) ? found : null;
  }

  function requireTableCell(editor){
    const cell = getActiveCell(editor);
    if (!cell){
      showToast("먼저 표 안의 셀을 클릭해 주세요.", true);
      return null;
    }
    return cell;
  }

  function addTableRow(editor){
    const cell = requireTableCell(editor);
    if (!cell) return;

    const row = cell.parentElement;
    const table = row.closest("table");
    if (!table) return;

    const newRow = document.createElement("tr");
    [...row.cells].forEach((sourceCell, idx) => {
      const tag = sourceCell.tagName === "TH" ? "TD" : sourceCell.tagName;
      const newCell = document.createElement(tag.toLowerCase());
      newCell.innerHTML = "&nbsp;";
      if (sourceCell.style.width) newCell.style.width = sourceCell.style.width;
      if (sourceCell.style.height) newCell.style.height = sourceCell.style.height;
      newRow.appendChild(newCell);
    });

    row.after(newRow);
    const target = newRow.cells[Math.min(cell.cellIndex, newRow.cells.length - 1)];
    setActiveCell(editor, target);
    markDirty();
  }

  function deleteTableRow(editor){
    const cell = requireTableCell(editor);
    if (!cell) return;

    const row = cell.parentElement;
    const table = row.closest("table");
    if (!table) return;

    const rows = [...table.rows];
    if (rows.length <= 1){
      showToast("표에는 최소 한 개의 행이 필요합니다.", true);
      return;
    }

    const rowIndex = rows.indexOf(row);
    row.remove();

    const remainRows = [...table.rows];
    const targetRow = remainRows[Math.min(rowIndex, remainRows.length - 1)];
    if (targetRow?.cells.length){
      setActiveCell(editor, targetRow.cells[Math.min(cell.cellIndex, targetRow.cells.length - 1)]);
    }
    markDirty();
  }

  function addTableColumn(editor){
    const cell = requireTableCell(editor);
    if (!cell) return;

    const table = cell.closest("table");
    if (!table) return;
    const colIndex = cell.cellIndex;

    [...table.rows].forEach(row => {
      const ref = row.cells[Math.min(colIndex, row.cells.length - 1)];
      const tag = ref?.tagName === "TH" ? "th" : "td";
      const newCell = document.createElement(tag);
      newCell.innerHTML = "&nbsp;";
      if (ref?.style.height) newCell.style.height = ref.style.height;

      const before = row.cells[colIndex + 1] || null;
      row.insertBefore(newCell, before);
    });

    const currentRow = cell.parentElement;
    setActiveCell(editor, currentRow.cells[colIndex + 1] || currentRow.cells[colIndex]);
    markDirty();
  }

  function deleteTableColumn(editor){
    const cell = requireTableCell(editor);
    if (!cell) return;

    const table = cell.closest("table");
    if (!table) return;

    const maxCols = Math.max(...[...table.rows].map(row => row.cells.length));
    if (maxCols <= 1){
      showToast("표에는 최소 한 개의 열이 필요합니다.", true);
      return;
    }

    const colIndex = cell.cellIndex;
    const currentRowIndex = [...table.rows].indexOf(cell.parentElement);

    [...table.rows].forEach(row => {
      if (row.cells[colIndex]) row.deleteCell(colIndex);
    });

    const targetRow = table.rows[Math.min(currentRowIndex, table.rows.length - 1)];
    if (targetRow?.cells.length){
      setActiveCell(editor, targetRow.cells[Math.min(colIndex, targetRow.cells.length - 1)]);
    }
    markDirty();
  }

  function normalizeCssSize(value){
    const raw = String(value ?? "").trim();
    if (!raw) return "";
    if (/^\d+(\.\d+)?$/.test(raw)) return `${raw}px`;
    if (/^\d+(\.\d+)?(px|%|em|rem|cm|mm)$/i.test(raw)) return raw;
    return null;
  }

  function setTableCellSize(editor){
    const cell = requireTableCell(editor);
    if (!cell) return;

    const widthInput = prompt(
      "선택 셀의 폭을 입력하세요. 예: 160, 30%, 4cm\\n비워두면 폭 설정을 해제합니다.",
      cell.style.width || ""
    );
    if (widthInput === null) return;

    const width = normalizeCssSize(widthInput);
    if (width === null){
      showToast("셀 폭 형식이 올바르지 않습니다.", true);
      return;
    }

    const heightInput = prompt(
      "선택 셀의 높이를 입력하세요. 예: 45, 1.5cm\\n비워두면 높이 설정을 해제합니다.",
      cell.style.height || ""
    );
    if (heightInput === null) return;

    const height = normalizeCssSize(heightInput);
    if (height === null){
      showToast("셀 높이 형식이 올바르지 않습니다.", true);
      return;
    }

    cell.style.width = width;
    cell.style.height = height;

    const table = cell.closest("table");
    if (table && width) table.style.tableLayout = "fixed";

    markDirty();
    showToast("셀 크기를 적용했습니다.");
  }

  async function uploadImage(file){
    const form = new FormData();
    form.append("manual_id", cfg.manualId);
    form.append("file", file);

    const res = await fetch(cfg.uploadImageUrl, {
      method:"POST",
      body:form
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.message || "이미지 업로드에 실패했습니다.");
    return data;
  }

  function imageFigureHtml(url, filename){
    return `
      <figure class="manual-image image-size-100 image-align-center"
              data-filename="${escapeHtml(filename)}" contenteditable="false">
        <img src="${escapeHtml(url)}" alt="" data-filename="${escapeHtml(filename)}">
        <figcaption contenteditable="true">이미지 설명을 입력할 수 있습니다.</figcaption>
      </figure>
      <p><br></p>
    `;
  }

  function setImageSize(figure, size){
    [...figure.classList]
      .filter(cls => /^image-size-\d+$/.test(cls))
      .forEach(cls => figure.classList.remove(cls));
    figure.classList.add(`image-size-${size}`);
    markDirty();
  }

  function setImageAlign(figure, align){
    [...figure.classList]
      .filter(cls => /^image-align-/.test(cls))
      .forEach(cls => figure.classList.remove(cls));
    figure.classList.add(`image-align-${align}`);
    markDirty();
  }

  function decorateImages(scope){
    scope.querySelectorAll("figure.manual-image").forEach(figure => {
      if (![...figure.classList].some(c => /^image-size-\d+$/.test(c))){
        figure.classList.add("image-size-100");
      }
      if (![...figure.classList].some(c => /^image-align-/.test(c))){
        figure.classList.add("image-align-center");
      }

      if (figure.querySelector(".manual-image-tools")) return;

      figure.setAttribute("contenteditable","false");

      const img = figure.querySelector("img");
      let filename = figure.dataset.filename || img?.dataset.filename || "";
      if (!filename && img){
        const parts = img.src.split("/");
        filename = decodeURIComponent(parts[parts.length - 1] || "");
        figure.dataset.filename = filename;
        img.dataset.filename = filename;
      }

      const currentSizeClass = [...figure.classList].find(c => /^image-size-\d+$/.test(c)) || "image-size-100";
      const currentSize = currentSizeClass.replace("image-size-", "");

      const tools = document.createElement("div");
      tools.className = "manual-image-tools";
      tools.innerHTML = `
        <select data-image-action="size" title="이미지 크기">
          <option value="25" ${currentSize === "25" ? "selected" : ""}>25%</option>
          <option value="50" ${currentSize === "50" ? "selected" : ""}>50%</option>
          <option value="75" ${currentSize === "75" ? "selected" : ""}>75%</option>
          <option value="100" ${currentSize === "100" ? "selected" : ""}>100%</option>
        </select>
        <button type="button" data-image-action="align-left" title="왼쪽 정렬">←</button>
        <button type="button" data-image-action="align-center" title="가운데 정렬">↔</button>
        <button type="button" data-image-action="align-right" title="오른쪽 정렬">→</button>
        <button type="button" data-image-action="replace">교체</button>
        <button type="button" data-image-action="remove">삭제</button>
      `;
      figure.appendChild(tools);

      const caption = figure.querySelector("figcaption");
      if (caption) caption.setAttribute("contenteditable","true");

      tools.querySelector('select[data-image-action="size"]').addEventListener("change", e => {
        setImageSize(figure, e.target.value);
      });

      tools.addEventListener("click", async e => {
        const action = e.target.dataset.imageAction;
        if (!action || action === "size") return;

        if (action === "align-left") setImageAlign(figure, "left");
        if (action === "align-center") setImageAlign(figure, "center");
        if (action === "align-right") setImageAlign(figure, "right");

        if (action === "remove"){
          if (!confirm("이 이미지를 삭제할까요?")) return;
          const stored = figure.dataset.filename;
          if (stored){
            try{
              await fetch(cfg.deleteImageUrl, {
                method:"POST",
                headers:{"Content-Type":"application/json"},
                body:JSON.stringify({manual_id:cfg.manualId, filename:stored})
              });
            }catch(_){}
          }
          figure.remove();
          markDirty();
        }

        if (action === "replace"){
          const input = document.createElement("input");
          input.type = "file";
          input.accept = "image/*";
          input.addEventListener("change", async () => {
            const file = input.files?.[0];
            if (!file) return;
            try{
              showToast("이미지를 업로드하고 있습니다.");
              const uploaded = await uploadImage(file);

              const old = figure.dataset.filename;
              if (old){
                fetch(cfg.deleteImageUrl, {
                  method:"POST",
                  headers:{"Content-Type":"application/json"},
                  body:JSON.stringify({manual_id:cfg.manualId, filename:old})
                }).catch(() => {});
              }

              figure.dataset.filename = uploaded.filename;
              if (img){
                img.src = uploaded.url;
                img.dataset.filename = uploaded.filename;
              }
              markDirty();
              showToast("이미지를 교체했습니다.");
            }catch(err){
              showToast(err.message, true);
            }
          }, {once:true});
          input.click();
        }
      });
    });
  }

  function handleToolbar(section, e){
    const button = e.target.closest("button");
    if (!button) return;
    e.preventDefault();

    const editor = section.querySelector(".saedam-rich-editor");
    editor.focus();
    restoreSelection(editor);

    if (button.dataset.command){
      document.execCommand(button.dataset.command, false, null);
      rememberSelection(editor);
      markDirty();
      return;
    }

    if (button.dataset.format){
      document.execCommand("formatBlock", false, button.dataset.format);
      rememberSelection(editor);
      markDirty();
      return;
    }

    const action = button.dataset.action;

    if (action === "link"){
      const url = prompt("연결할 주소를 입력하세요.", "https://");
      if (!url) return;
      document.execCommand("createLink", false, url);
      rememberSelection(editor);
      markDirty();
    }

    if (action === "table"){
      insertHtml(editor, `
        <table class="manual-edit-table">
          <tbody>
            <tr><th>항목</th><th>내용</th></tr>
            <tr><td>항목 1</td><td>내용을 입력하세요.</td></tr>
            <tr><td>항목 2</td><td>내용을 입력하세요.</td></tr>
          </tbody>
        </table><p><br></p>
      `);
    }

    if (action === "table-row-add") addTableRow(editor);
    if (action === "table-row-delete") deleteTableRow(editor);
    if (action === "table-col-add") addTableColumn(editor);
    if (action === "table-col-delete") deleteTableColumn(editor);
    if (action === "table-cell-size") setTableCellSize(editor);

    if (["note","warn","danger"].includes(action)){
      const label = action === "note" ? "안내" : action === "warn" ? "주의" : "경고";
      insertHtml(editor, `<div class="callout ${action}"><strong>${label}</strong><br>내용을 입력하세요.</div><p><br></p>`);
    }

    if (action === "image"){
      const input = section.querySelector(".section-image-input");
      input.value = "";
      input.click();
    }
  }

  function bindSection(section){
    const title = section.querySelector(".section-title-input");
    const desc = section.querySelector(".section-desc-input");
    const editor = section.querySelector(".saedam-rich-editor");
    const toolbar = section.querySelector(".saedam-editor-toolbar");
    const imageInput = section.querySelector(".section-image-input");
    const fontSizeSelect = section.querySelector(".manual-font-size-select");
    const fontColorInput = section.querySelector(".manual-font-color-input");

    title.addEventListener("input", () => {
      syncTocTitles();
      markDirty();
    });
    desc.addEventListener("input", markDirty);
    editor.addEventListener("input", markDirty);

    editor.addEventListener("keyup", () => rememberSelection(editor));
    editor.addEventListener("mouseup", () => rememberSelection(editor));
    editor.addEventListener("click", e => {
      const cell = e.target.closest("td,th");
      if (cell && editor.contains(cell)){
        setActiveCell(editor, cell);
      }else if (!e.target.closest(".manual-image")){
        setActiveCell(editor, null);
      }
      rememberSelection(editor);
    });

    editor.addEventListener("focus", () => {
      document.querySelectorAll(".manual-edit-section.focused").forEach(el => el.classList.remove("focused"));
      section.classList.add("focused");
    });

    section.querySelector(".section-delete-btn").addEventListener("click", () => removeSection(section));

    toolbar.addEventListener("mousedown", e => {
      if (e.target.closest("button")) e.preventDefault();
    });
    toolbar.addEventListener("click", e => handleToolbar(section, e));

    fontSizeSelect.addEventListener("mousedown", () => rememberSelection(editor));
    fontSizeSelect.addEventListener("change", () => {
      if (fontSizeSelect.value) applyInlineStyle(editor, "fontSize", fontSizeSelect.value);
      fontSizeSelect.value = "";
    });

    fontColorInput.addEventListener("mousedown", () => rememberSelection(editor));
    fontColorInput.addEventListener("change", () => {
      applyInlineStyle(editor, "color", fontColorInput.value);
    });

    imageInput.addEventListener("change", async () => {
      const file = imageInput.files?.[0];
      if (!file) return;
      try{
        showToast("이미지를 업로드하고 있습니다.");
        const uploaded = await uploadImage(file);
        insertHtml(editor, imageFigureHtml(uploaded.url, uploaded.filename));
        decorateImages(section);
        showToast("이미지를 삽입했습니다.");
      }catch(err){
        showToast(err.message, true);
      }
    });
  }

  function cleanEditorHtml(editor){
    const clone = editor.cloneNode(true);
    clone.querySelectorAll(".manual-image-tools").forEach(el => el.remove());
    clone.querySelectorAll(".manual-cell-selected").forEach(cell => cell.classList.remove("manual-cell-selected"));
    clone.querySelectorAll("figure.manual-image").forEach(fig => {
      fig.setAttribute("contenteditable","false");
      fig.querySelectorAll("figcaption").forEach(c => c.removeAttribute("contenteditable"));
    });
    return clone.innerHTML.trim();
  }

  function serialize(){
    return {
      title:titleInput.value.trim(),
      description:descInput.value.trim(),
      sections:sectionEls().map(section => ({
        title:section.querySelector(".section-title-input").value.trim(),
        description:section.querySelector(".section-desc-input").value.trim(),
        content_html:cleanEditorHtml(section.querySelector(".saedam-rich-editor"))
      }))
    };
  }

  async function save({silent=false}={}){
    if (saving) return false;
    saving = true;
    if (!silent) saveBtn.disabled = true;
    saveState.textContent = "저장중";

    try{
      const res = await fetch(cfg.saveUrl, {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify(serialize())
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.message || "저장에 실패했습니다.");

      dirty = false;
      saveState.textContent = cfg.status === "published" ? "작성완료" : "저장됨";
      if (!silent) showToast("임시저장했습니다.");
      return true;
    }catch(err){
      saveState.textContent = "저장오류";
      if (!silent) showToast(err.message, true);
      return false;
    }finally{
      saving = false;
      saveBtn.disabled = false;
    }
  }

  async function preview(){
    const ok = await save();
    if (!ok) return;
    window.open(cfg.previewUrl, "_blank", "noopener");
  }

  async function publish(){
    const ok = await save();
    if (!ok) return;

    publishBtn.disabled = true;
    try{
      const res = await fetch(cfg.publishUrl, {method:"POST"});
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.message || "작성완료 처리에 실패했습니다.");

      cfg.status = "published";
      saveState.textContent = "작성완료";
      showToast("메뉴얼 작성이 완료되었습니다.");
      setTimeout(() => {
        window.location.href = data.view_url || cfg.listUrl;
      }, 450);
    }catch(err){
      showToast(err.message, true);
    }finally{
      publishBtn.disabled = false;
    }
  }

  function confirmReplaceBeforeImport(){
    const currentHasContent = sectionEls().some(s => {
      const body = s.querySelector(".saedam-rich-editor").innerText.trim();
      return body || s.querySelector(".section-title-input").value.trim() !== "1. 새 목차";
    });
    if (!currentHasContent) return true;
    return confirm("현재 작성 내용을 불러온 파일 내용으로 바꿀까요?");
  }

  function replaceSectionsFromFile(data){
    sectionContainer.innerHTML = "";
    tocList.innerHTML = "";

    if (data.title) titleInput.value = data.title;
    if (data.description) descInput.value = data.description;
    (data.sections || []).forEach(sec => createSection(sec));
    if (!sectionEls().length) createSection();

    renumber();
    syncTocTitles();
    markDirty();

    const imageCount = Number(data.image_count || 0);
    showToast(imageCount
      ? `파일 내용을 불러왔습니다. (이미지 ${imageCount}개 포함) 임시저장을 눌러 저장하세요.`
      : "파일 내용을 불러왔습니다. 임시저장을 눌러 저장하세요.");
    return true;
  }

  async function importFile(file){
    // 서버에 파일을 보내기 전에 먼저 확인받아, 취소한 경우 이미지가 불필요하게 저장되지 않도록 합니다.
    if (!confirmReplaceBeforeImport()){
      txtInput.value = "";
      return;
    }

    const form = new FormData();
    form.append("file", file);
    form.append("manual_id", cfg.manualId);
    try{
      showToast("파일을 읽고 있습니다.");
      const res = await fetch(cfg.importFileUrl || cfg.uploadTxtUrl, {method:"POST", body:form});
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.message || "파일 불러오기에 실패했습니다.");
      replaceSectionsFromFile(data);
    }catch(err){
      showToast(err.message, true);
    }finally{
      txtInput.value = "";
    }
  }

  async function deleteManual(){
    if (!confirm("이 메뉴얼을 완전히 삭제할까요?\\n작성 내용과 업로드 이미지도 함께 삭제됩니다.")) return;
    try{
      const res = await fetch(cfg.deleteUrl, {method:"POST"});
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.message || "삭제하지 못했습니다.");
      window.location.href = cfg.listUrl;
    }catch(err){
      showToast(err.message, true);
    }
  }

  function bindExisting(){
    tocEls().forEach(bindTocItem);
    sectionEls().forEach(section => {
      bindSection(section);
      decorateImages(section);
    });
    renumber();
    syncTocTitles();
  }

  document.getElementById("addSectionBtn").addEventListener("click", () => createSection());
  document.getElementById("addSectionBtnBottom").addEventListener("click", () => createSection());
  document.getElementById("deleteManualBtn").addEventListener("click", deleteManual);
  saveBtn.addEventListener("click", () => save());
  previewBtn.addEventListener("click", preview);
  publishBtn.addEventListener("click", publish);

  titleInput.addEventListener("input", markDirty);
  descInput.addEventListener("input", markDirty);

  txtInput.addEventListener("change", () => {
    const file = txtInput.files?.[0];
    if (file) importFile(file);
  });

  window.addEventListener("beforeunload", e => {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });

  sectionObserver = new IntersectionObserver(entries => {
    const visible = entries
      .filter(x => x.isIntersecting)
      .sort((a,b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    const key = visible.target.dataset.sectionKey;
    tocEls().forEach(item => item.classList.toggle("active", item.dataset.sectionKey === key));
  }, {rootMargin:"-20% 0px -65% 0px", threshold:[0,.1,.4]});

  bindExisting();
  sectionEls().forEach(section => sectionObserver.observe(section));

  setInterval(() => {
    if (dirty && !saving) save({silent:true});
  }, 60000);
})();
