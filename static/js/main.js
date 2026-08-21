(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});

  R.REGISTRY = [
    { tab: "devices", title: "设备管理", render: () => R.pages.devices.render() },
    { tab: "files", title: "文件管理", render: () => R.pages.files.render() },
    { tab: "term", title: "终端", render: () => R.pages.term.render() },
    { tab: "monitor", title: "监控",
      render: () => R.pages.monitor.render(), deactivate: () => R.pages.monitor.deactivate() },
    { tab: "process", title: "进程",
      render: () => R.pages.process.render(), deactivate: () => R.pages.process.deactivate() },
    { tab: "diag", title: "诊断",
      render: () => R.pages.diag.render(), deactivate: () => R.pages.diag.deactivate() },
    { tab: "peripherals", title: "外设",
      render: () => R.pages.peripherals.render(), deactivate: () => R.pages.peripherals.deactivate() },
    { tab: "agent", title: "智能 Agent",
      render: () => R.pages.agent.render(), deactivate: () => R.pages.agent.deactivate() },
    { tab: "audit", title: "审计",
      render: () => R.pages.audit.render(), deactivate: () => R.pages.audit.deactivate() },
    { tab: "keys", title: "密钥",
      render: () => R.pages.keys.render(), deactivate: () => R.pages.keys.deactivate() },
    { tab: "groups", title: "分组",
      render: () => R.pages.groups.render(), deactivate: () => R.pages.groups.deactivate() },
    { tab: "logcenter", title: "日志中心",
      render: () => R.pages.logcenter.render(), deactivate: () => R.pages.logcenter.deactivate() },
  ];

  let activeItem = null;

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach(b =>
      b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".pane").forEach(p =>
      p.classList.toggle("active", p.id === "tab-" + name));
    const activeBtn = document.querySelector('.tab[data-tab="' + name + '"]');
    if (activeBtn) {
      const group = activeBtn.closest("details");
      if (group) {
        document.querySelectorAll(".sidebar details").forEach(d => {
          d.open = d === group;
        });
      }
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    for (const item of R.REGISTRY) {
      const page = R.pages[item.tab];
      if (!page || !page.bind) continue;
      try {
        page.bind();
      } catch (e) {
        console.error("[rkss] bind " + item.tab + " 失败", e);
      }
    }

    document.querySelector(".sidebar").addEventListener("click", (e) => {
      const b = e.target.closest ? e.target.closest(".tab") : null;
      if (!b) return;
      if (activeItem && activeItem.deactivate) activeItem.deactivate();
      activeItem = null;
    }, true);

    for (const item of R.REGISTRY) {
      document.querySelectorAll(".tab[data-tab='" + item.tab + "']").forEach(b =>
        b.addEventListener("click", () => {
          activeItem = item;
          switchTab(item.tab);
          try {
            item.render();
          } catch (e) {
            console.error("[rkss] render " + item.tab + " 失败", e);
          }
        }));
    }

    const initial = document.querySelector(".tab.active");
    if (initial) {
      const item = R.REGISTRY.find(i => i.tab === initial.dataset.tab);
      if (item) {
        activeItem = item;
        try {
          item.render();
        } catch (e) {
          console.error("[rkss] render " + item.tab + " 失败", e);
        }
      }
    }
  });
})();
