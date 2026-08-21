(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});

  const store = {
    devices: [],
    deviceMap: {},
    hostEnv: null,
    _jobs: [],
    setDevices(list) {
      store.devices = list || [];
      store.deviceMap = {};
      for (const d of store.devices) store.deviceMap[d.id] = d;
    },
  };

  const state = {
    fm: {
      device: "local",
      paths: { local: "~", remote: "/" },
      hidden: false,
      sel: { local: new Set(), remote: new Set() },
      jobsOpen: false,
      jobsTimer: null,
    },
    term: {
      device: "local",
      jobId: null,
      jobDevice: null,
      offset: 0,
      pollTimer: null,
      pollToken: 0,
      pollInFlight: false,
      history: [],
      histIdx: -1,
      follow: true,
    },
  };

  R.store = store;
  R.state = state;
})();
