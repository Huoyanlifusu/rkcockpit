(function () {
  "use strict";
  const R = (window.RKS = window.RKS || {});

  async function responseError(r) {
    let data = null;
    try { data = await r.json(); } catch (e) { /* ignore */ }
    if (r.status === 401 && R.auth) {
      R.auth.requireLogin((data && data.error) || "需要管理员登录");
    }
    return new Error((data && data.error) || ("HTTP " + r.status));
  }

  async function fetchApi(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw await responseError(r);
    const j = await r.json();
    if (j && j.ok === false) throw new Error(j.error || "请求失败");
    return j;
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw await responseError(r);
    return r.json();
  }

  R.api = R.api || {};
  R.api.fetch = fetchApi;
  R.api.api = api;
  R.api.responseError = responseError;
})();
