(function () {
  var addForm = document.getElementById("add-form");
  var titleInput = document.getElementById("title");
  var list = document.getElementById("task-list");
  var empty = document.getElementById("empty");
  var errorBox = document.getElementById("error");
  var status = document.getElementById("status-count");
  var tip = document.getElementById("field-tip");
  var tipTitle = document.getElementById("field-tip-title");
  var tipText = document.getElementById("field-tip-text");
  var tipMode = null;
  var caret = document.getElementById("pixel-caret");
  var caretMeasure = document.createElement("canvas").getContext("2d");

  addForm.noValidate = true;

  function placeCaret() {
    if (document.activeElement !== titleInput) {
      caret.hidden = true;
      return;
    }
    var style = getComputedStyle(titleInput);
    caretMeasure.font = style.font;
    var textWidth = caretMeasure.measureText(titleInput.value.slice(0, titleInput.selectionStart || 0)).width;
    var caretHeight = 18;
    caret.hidden = false;
    caret.style.left = (parseFloat(style.paddingLeft) + textWidth - titleInput.scrollLeft) + "px";
    caret.style.top = ((titleInput.clientHeight - caretHeight) / 2) + "px";
  }

  function showTip(mode, title, text) {
    tipMode = mode;
    tipTitle.textContent = title;
    tipText.textContent = text;
    tip.hidden = false;
  }

  function hideTip(mode) {
    if (mode && tipMode !== mode) {
      return;
    }
    tip.hidden = true;
    tipMode = null;
  }

  function showError(message) {
    if (!message) {
      errorBox.hidden = true;
      errorBox.textContent = "";
      return;
    }
    errorBox.hidden = false;
    errorBox.textContent = message;
  }

  function updateCount() {
    var count = list.querySelectorAll(".task-row").length;
    status.textContent = "Задач: " + count;
    empty.hidden = count !== 0;
    list.hidden = count === 0;
  }

  function reloadTasks() {
    return fetch("/tasks", { headers: { Accept: "application/json" } }).then(function (response) {
      if (!response.ok) {
        return null;
      }
      return response.json().then(function (tasks) {
        list.replaceChildren();
        tasks.forEach(function (task) {
          list.appendChild(taskRow(task));
        });
        updateCount();
      });
    }).catch(function () {
      showError("Не удалось загрузить задачи.");
    });
  }

  function toggleTask(form) {
    var item = form.closest(".task-item");
    var checkbox = form.querySelector('input[type="checkbox"]');
    var previous = item.classList.contains("done");
    return fetch(form.action, {
      method: "POST",
      headers: { Accept: "application/json" }
    }).then(function (response) {
      if (!response.ok) {
        checkbox.checked = previous;
        return detailFrom(response).then(showError);
      }
      return response.json().then(function (task) {
        showError("");
        checkbox.checked = task.done;
        item.classList.toggle("done", task.done);
      });
    }).catch(function () {
      checkbox.checked = previous;
      showError("Не удалось выполнить действие.");
    });
  }

  function detailFrom(response) {
    return response.json().then(
      function (data) {
        return (data && data.detail) || "Не удалось выполнить действие.";
      },
      function () {
        return "Не удалось выполнить действие.";
      }
    );
  }

  var rowTemplate = document.getElementById("task-row-template");

  function taskRow(task) {
    var item = rowTemplate.content.firstElementChild.cloneNode(true);
    var base = "/tasks/" + encodeURIComponent(task.id);
    item.dataset.id = task.id;
    item.classList.toggle("done", Boolean(task.done));
    item.querySelector(".edit-slot").action = base + "/edit";
    item.querySelector(".edit-title").value = task.title;
    item.querySelector(".toggle-form").action = base + "/toggle";
    item.querySelector('input[type="checkbox"]').checked = Boolean(task.done);
    item.querySelector(".task-title").textContent = task.title;
    item.querySelector(".delete-form").action = base + "/delete";
    return item;
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest(".edit-button");
    if (!button) {
      return;
    }
    var item = button.closest(".task-item");
    var slot = item.querySelector(".edit-slot");
    var input = slot.querySelector(".edit-title");
    if (!slot.hidden) {
      slot.requestSubmit();
      return;
    }
    document.querySelectorAll(".edit-slot").forEach(function (other) {
      if (other !== slot) {
        other.hidden = true;
      }
    });
    input.value = item.querySelector(".task-title").textContent;
    slot.hidden = false;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });

  document.querySelector(".field-group").addEventListener("pointerleave", function () {
    hideTip();
  });

  titleInput.addEventListener("input", function () {
    hideTip();
    placeCaret();
  });

  ["keyup", "click", "focus", "select"].forEach(function (eventName) {
    titleInput.addEventListener(eventName, placeCaret);
  });

  titleInput.addEventListener("blur", function () {
    caret.hidden = true;
  });

  document.addEventListener("selectionchange", function () {
    if (document.activeElement === titleInput) {
      placeCaret();
    }
  });

  document.addEventListener("pointerdown", function (event) {
    if (tip.hidden || addForm.contains(event.target)) {
      return;
    }
    hideTip();
  });

  addForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var title = new FormData(addForm).get("title");
    if (!String(title || "").trim()) {
      showTip("warn", "Внимание", "Заполните это поле");
      titleInput.focus();
      return;
    }
    hideTip("warn");
    fetch("/tasks", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ title: title })
    }).then(function (response) {
      if (!response.ok) {
        return detailFrom(response).then(function (message) {
          showError(message);
        });
      }
      return response.json().then(function (task) {
        showError("");
        addForm.reset();
        list.appendChild(taskRow(task));
        updateCount();
        titleInput.focus();
      });
    }).catch(function () {
      showError("Не удалось выполнить действие.");
    });
  });

  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input || input.type !== "checkbox" || !input.form || !input.form.classList.contains("toggle-form")) {
      return;
    }
    toggleTask(input.form);
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.id === "add-form") {
      return;
    }
    if (form.classList.contains("edit-slot")) {
      event.preventDefault();
      var editItem = form.closest(".task-item");
      var editInput = form.querySelector(".edit-title");
      fetch(form.action, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ title: editInput.value })
      }).then(function (response) {
        if (!response.ok) {
          return detailFrom(response).then(showError);
        }
        return response.json().then(function (task) {
          showError("");
          editItem.querySelector(".task-title").textContent = task.title;
          form.hidden = true;
        });
      }).catch(function () {
        showError("Не удалось выполнить действие.");
      });
      return;
    }
    if (form.classList.contains("toggle-form")) {
      event.preventDefault();
      toggleTask(form);
      return;
    }
    if (!form.classList.contains("delete-form")) {
      return;
    }
    event.preventDefault();
    fetch(form.action, {
      method: "POST",
      headers: { Accept: "application/json" }
    }).then(function (response) {
      if (!response.ok) {
        return detailFrom(response).then(showError);
      }
      showError("");
      form.closest(".task-item").remove();
      updateCount();
    }).catch(function () {
      showError("Не удалось выполнить действие.");
    });
  });

  var windowDrag = null;
  var windowHomes = [];

  function bringToFront(win) {
    var windows = Array.prototype.slice.call(document.querySelectorAll(".win[data-window]"));
    windows.sort(function (a, b) {
      return (parseInt(a.style.zIndex, 10) || 0) - (parseInt(b.style.zIndex, 10) || 0);
    });
    var z = 10;
    windows.forEach(function (other) {
      if (other === win) {
        return;
      }
      other.style.zIndex = String(z);
      z += 1;
      other.classList.remove("is-active");
    });
    win.style.zIndex = String(z);
    win.classList.add("is-active");
    var button = document.querySelector('.taskbar-button[data-target="' + win.id + '"]');
    document.querySelectorAll(".taskbar-button").forEach(function (item) {
      item.classList.toggle("is-active", item === button && !win.hidden);
    });
  }

  function rememberWindow(win) {
    win.dataset.left = win.style.left;
    win.dataset.top = win.style.top;
    win.dataset.width = win.style.width;
    win.dataset.height = win.style.height;
  }

  function restoreWindowBox(win) {
    win.classList.remove("is-maximized");
    win.style.left = win.dataset.left || win.style.left;
    win.style.top = win.dataset.top || win.style.top;
    win.style.width = win.dataset.width || win.style.width;
    win.style.height = win.dataset.height || "";
    var maximize = win.querySelector('[data-action="maximize"]');
    if (maximize) {
      maximize.setAttribute("aria-label", "Развернуть");
    }
    fitTaskList();
  }

  function fitTaskList() {
    var win = document.getElementById("window-task-list");
    if (!win) {
      return;
    }
    if (win.classList.contains("is-maximized")) {
      list.style.maxHeight = "";
      return;
    }
    var homeTop = null;
    windowHomes.forEach(function (home) {
      if (home.id === "window-task-list") {
        homeTop = home.top;
      }
    });
    if (homeTop === null) {
      return;
    }
    var title = win.querySelector(".win-title");
    var body = win.querySelector(".win-body");
    var bodyStyle = getComputedStyle(body);
    var winStyle = getComputedStyle(win);
    var pad = parseFloat(bodyStyle.paddingTop) + parseFloat(bodyStyle.paddingBottom);
    var winPad = parseFloat(winStyle.paddingTop) + parseFloat(winStyle.paddingBottom);
    var statusSpace = status.offsetHeight + 12;
    var available = window.innerHeight - 40 - homeTop - title.offsetHeight - pad - winPad - statusSpace - 8;
    list.style.maxHeight = Math.max(72, Math.floor(available)) + "px";
  }

  function maximizeWindow(win) {
    if (!win.classList.contains("is-maximized")) {
      rememberWindow(win);
      win.classList.add("is-maximized");
      win.style.left = "0px";
      win.style.top = "0px";
      win.style.width = "100vw";
      win.style.height = "calc(100vh - 40px)";
      win.querySelector('[data-action="maximize"]').setAttribute("aria-label", "Восстановить");
      list.style.maxHeight = "";
    } else {
      restoreWindowBox(win);
    }
    win.hidden = false;
    bringToFront(win);
    fitTaskList();
  }

  function showWindow(win) {
    win.hidden = false;
    bringToFront(win);
  }

  function hideWindow(win) {
    win.hidden = true;
    var button = document.querySelector('.taskbar-button[data-target="' + win.id + '"]');
    if (button) {
      button.classList.remove("is-active");
    }
  }

  function applyHome(win, home, index) {
    win.classList.remove("is-maximized");
    win.hidden = false;
    win.style.position = "fixed";
    win.style.left = home.left + "px";
    win.style.top = home.top + "px";
    win.style.width = home.width + "px";
    win.style.height = "";
    win.style.margin = "0";
    win.style.zIndex = String(10 + index);
    win.classList.toggle("is-active", index === 0);
    var maximize = win.querySelector('[data-action="maximize"]');
    if (maximize) {
      maximize.setAttribute("aria-label", "Развернуть");
    }
    var button = document.querySelector('.taskbar-button[data-target="' + win.id + '"]');
    if (button) {
      button.classList.add("is-active");
    }
  }

  function mountWindows() {
    windowHomes = [];
    document.querySelectorAll(".win[data-window]").forEach(function (win) {
      var rect = win.getBoundingClientRect();
      windowHomes.push({
        id: win.id,
        left: rect.left,
        top: rect.top,
        width: rect.width
      });
    });
    resetWindows();
  }

  function resetWindows() {
    document.querySelectorAll(".win[data-window]").forEach(function (win, index) {
      var home = windowHomes[index];
      if (!home) {
        return;
      }
      applyHome(win, home, index);
    });
    list.style.maxHeight = "";
    fitTaskList();
  }

  document.querySelectorAll(".win[data-window]").forEach(function (win) {
    var title = win.querySelector(".win-title");
    title.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || event.target.closest(".win-control")) {
        return;
      }
      if (win.classList.contains("is-maximized")) {
        return;
      }
      var rect = win.getBoundingClientRect();
      windowDrag = {
        win: win,
        dx: event.clientX - rect.left,
        dy: event.clientY - rect.top
      };
      bringToFront(win);
      title.setPointerCapture(event.pointerId);
    });
    title.addEventListener("pointermove", function (event) {
      if (!windowDrag || windowDrag.win !== win) {
        return;
      }
      var maxLeft = window.innerWidth - 120;
      var maxTop = window.innerHeight - 80;
      var left = Math.min(Math.max(0, event.clientX - windowDrag.dx), maxLeft);
      var top = Math.min(Math.max(0, event.clientY - windowDrag.dy), maxTop);
      win.style.left = left + "px";
      win.style.top = top + "px";
    });
    title.addEventListener("pointerup", function () {
      windowDrag = null;
    });
    title.addEventListener("dblclick", function (event) {
      if (event.target.closest(".win-control")) {
        return;
      }
      maximizeWindow(win);
    });
    win.querySelectorAll(".win-control").forEach(function (control) {
      control.addEventListener("click", function (event) {
        event.stopPropagation();
        var action = control.dataset.action;
        if (action === "close" || action === "minimize") {
          hideWindow(win);
          return;
        }
        if (action === "maximize") {
          maximizeWindow(win);
        }
      });
    });
  });

  document.querySelectorAll(".taskbar-button").forEach(function (button) {
    button.addEventListener("click", function () {
      var win = document.getElementById(button.dataset.target);
      if (!win) {
        return;
      }
      if (!win.hidden && win.classList.contains("is-active")) {
        hideWindow(win);
        return;
      }
      showWindow(win);
    });
  });

  mountWindows();
  window.addEventListener("resize", fitTaskList);

  var profileButton = document.getElementById("profile-button");
  var accountModal = document.getElementById("account-modal");
  var accountTitle = document.getElementById("account-title");
  var loginForm = document.getElementById("login-form");
  var registerForm = document.getElementById("register-form");
  var cabinetView = document.getElementById("cabinet-view");
  var cabinetLogin = document.getElementById("cabinet-login");
  var accountError = document.getElementById("account-error");
  var currentLogin = document.body.dataset.login || "";

  function showAccountError(message) {
    if (!message) {
      accountError.hidden = true;
      accountError.textContent = "";
      return;
    }
    accountError.hidden = false;
    accountError.textContent = message;
  }

  function showAccountState(state) {
    loginForm.hidden = state !== "login";
    registerForm.hidden = state !== "register";
    cabinetView.hidden = state !== "cabinet";
    accountTitle.textContent = state === "register" ? "Регистрация" : state === "cabinet" ? "Личный кабинет" : "Вход";
    showAccountError("");
  }

  var accountWindow = accountModal.querySelector(".account-window");
  var accountDrag = null;

  function centerAccountWindow() {
    accountWindow.style.position = "fixed";
    accountWindow.style.margin = "0";
    accountWindow.style.transform = "none";
    var width = accountWindow.offsetWidth;
    var height = accountWindow.offsetHeight;
    var left = Math.max(0, Math.round((window.innerWidth - width) / 2));
    var top = Math.max(0, Math.round((window.innerHeight - height) / 2));
    accountWindow.style.left = left + "px";
    accountWindow.style.top = top + "px";
  }

  function openAccount() {
    showAccountState(currentLogin ? "cabinet" : "login");
    if (currentLogin) {
      cabinetLogin.textContent = currentLogin;
    }
    accountModal.hidden = false;
    centerAccountWindow();
  }

  accountTitle.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) {
      return;
    }
    var rect = accountWindow.getBoundingClientRect();
    accountDrag = {
      dx: event.clientX - rect.left,
      dy: event.clientY - rect.top
    };
    accountTitle.setPointerCapture(event.pointerId);
  });

  accountTitle.addEventListener("pointermove", function (event) {
    if (!accountDrag) {
      return;
    }
    var rect = accountWindow.getBoundingClientRect();
    var maxLeft = Math.max(0, window.innerWidth - Math.min(rect.width, 160));
    var maxTop = Math.max(0, window.innerHeight - 48);
    var left = Math.min(Math.max(0, event.clientX - accountDrag.dx), maxLeft);
    var top = Math.min(Math.max(0, event.clientY - accountDrag.dy), maxTop);
    accountWindow.style.left = left + "px";
    accountWindow.style.top = top + "px";
  });

  accountTitle.addEventListener("pointerup", function () {
    accountDrag = null;
  });
  accountTitle.addEventListener("pointercancel", function () {
    accountDrag = null;
  });

  function closeAccount() {
    accountModal.hidden = true;
  }

  function authRequest(url, login, password) {
    return fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ login: login, password: password })
    }).then(function (response) {
      return response.json().then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      }, function () {
        return { ok: false, status: response.status, data: {} };
      });
    });
  }

  profileButton.addEventListener("dblclick", function (event) {
    event.preventDefault();
    if (!accountModal.hidden) {
      closeAccount();
      return;
    }
    openAccount();
  });

  accountModal.addEventListener("click", function (event) {
    if (event.target.closest("[data-close]")) {
      closeAccount();
    }
  });

  document.getElementById("show-register").addEventListener("click", function () {
    showAccountState("register");
    document.getElementById("register-name").focus();
  });

  loginForm.addEventListener("submit", function (event) {
    event.preventDefault();
    authRequest("/auth/login", document.getElementById("login-name").value, document.getElementById("login-password").value).then(function (result) {
      if (!result.ok) {
        showAccountError((result.data && result.data.detail) || "Неверный логин или пароль.");
        return;
      }
      currentLogin = result.data.login;
      document.body.dataset.login = currentLogin;
      loginForm.reset();
      closeAccount();
      resetWindows();
      reloadTasks();
    }).catch(function () {
      showAccountError("Не удалось выполнить действие.");
    });
  });

  registerForm.addEventListener("submit", function (event) {
    event.preventDefault();
    authRequest("/auth/register", document.getElementById("register-name").value, document.getElementById("register-password").value).then(function (result) {
      if (!result.ok) {
        showAccountError((result.data && result.data.detail) || "Не удалось зарегистрироваться.");
        return;
      }
      currentLogin = result.data.login;
      document.body.dataset.login = currentLogin;
      registerForm.reset();
      closeAccount();
      resetWindows();
      reloadTasks();
    }).catch(function () {
      showAccountError("Не удалось выполнить действие.");
    });
  });

  function signOut(url) {
    fetch(url, { method: "POST", headers: { Accept: "application/json" } }).then(function (response) {
      if (!response.ok) {
        return detailFrom(response).then(showAccountError);
      }
      currentLogin = "";
      delete document.body.dataset.login;
      loginForm.reset();
      registerForm.reset();
      showAccountState("login");
      resetWindows();
      reloadTasks();
    }).catch(function () {
      showAccountError("Не удалось выполнить действие.");
    });
  }

  document.getElementById("logout-button").addEventListener("click", function () {
    signOut("/auth/logout");
  });

  document.getElementById("logout-all-button").addEventListener("click", function () {
    signOut("/auth/logout-all");
  });
})();
