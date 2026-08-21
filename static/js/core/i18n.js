/* Lightweight UI localization; Chinese strings are the canonical source. */
(function () {
  "use strict";
  const KEY = "rkss-language";
  const words = {
    "RK 设备运维控制台":"RK Device Operations Console","管理员登录":"Administrator sign-in","访问令牌":"Access token","登录":"Sign in",
    "设备":"Device","设备管理":"Devices","文件管理":"Files","终端":"Terminal","监控运维":"Operations","监控":"Monitoring","进程":"Processes",
    "诊断":"Diagnostics","外设":"Peripherals","智能 Agent":"AI Agent","日志中心":"Logs","管理":"Administration","密钥":"Keys","分组":"Groups","审计":"Audit",
    "新增设备":"Add device","自动发现":"Auto-discover","刷新":"Refresh","名称":"Name","类型":"Type","地址":"Address","认证":"Authentication","备注":"Notes",
    "状态":"Status","操作":"Actions","端口":"Port","用户":"User","密码":"Password","保存":"Save","取消":"Cancel","显示隐藏":"Show hidden",
    "新建文件夹":"New folder","上传文件…":"Upload file…","传输任务":"Transfers","上位机本地（local）":"Host (local)","上位机":"Host","本地（local）":"Local",
    "上级":"Up","新建":"New","重命名":"Rename","删除":"Delete","大小":"Size","修改时间":"Modified","权限":"Permissions","板卡":"Board",
    "运行":"Run","终止":"Terminate","清空":"Clear","窗口":"Window","开始采样":"Start sampling","停止采样":"Stop sampling","内存":"Memory","温度":"Temperature",
    "负载":"Load","排序":"Sort","顺序":"Order","降序":"Descending","升序":"Ascending","命令":"Command","关闭":"Close","行数":"Lines","过滤":"Filter",
    "自动跟随":"Auto-follow","新建会话":"New session","会话":"Sessions","未选择会话":"No session selected","发送":"Send","动作":"Action","结果":"Result",
    "全部":"All","时间范围":"Time range","最近 1h":"Last hour","最近 24h":"Last 24 hours","最近 7d":"Last 7 days","导出 CSV":"Export CSV",
    "时间":"Time","目标":"Target","详情":"Details","测试连接":"Test connection","编辑":"Edit","未知":"Unknown","未知错误":"Unknown error","失败":"Failed",
    "可达":"Reachable","已连接":"Connected","未授权":"Unauthorized","离线":"Offline","系统":"System","内核":"Kernel","型号":"Model","运行时长":"Uptime",
    "连接":"Connection","延迟":"Latency","主机名":"Hostname","全选":"Select all","反选":"Invert selection","添加所选":"Add selected","全部添加":"Add all",
    "SSH 用户":"SSH user","IP 规则":"IP rules","SN 规则":"Serial rules","添加 IP 规则":"Add IP rule","添加 SN 规则":"Add serial rule","导入结果":"Import result",
    "当前无运行任务":"No active tasks","暂无任务":"No tasks","暂无样本":"No samples","暂无审计记录":"No audit records","流式":"Streaming","执行中…":"Running…",
    "参数: ":"Arguments: ","结果: ":"Result: ","测试出流":"Test stream","出流测试中…":"Testing stream…","查看内核日志":"View kernel log","受限":"Restricted",
    "展开全部路径":"Expand full path","..（上级目录）":".. (parent directory)","本地根目录":"Local root directory","video 设备":"Video devices","USB 设备":"USB devices",
    "I2C 总线":"I2C buses","UART 串口":"UART ports","时钟 clk":"Clocks","看门狗 watchdog":"Watchdogs","电源 regulator":"Regulators","DMA 通道":"DMA channels",
    "→ 上传到设备":"→ Upload to device","← 下载到上位机":"← Download to host","cpu核数":"CPU cores","总数":"Total","杀":"Terminate",
    "源":"Source","跟随":"Follow","■ 停止跟随":"■ Stop following","▶ 跟随":"▶ Follow","只读工具":"Read-only tools",
    "未配置 LLM：请在配置目录 llm.json 填写 base_url 与 model（api_key 可选）":"LLM is not configured. Set base_url and model in llm.json (api_key is optional).",
    "请输入部署时生成的访问令牌。令牌不会保存在浏览器存储中。":"Enter the access token generated during deployment. It is not stored in the browser.",
    "RK 设备运维控制台 · 门户 :8080 · SSH/ADB 设备管理":"RK Device Operations Console · Portal :8080 · SSH/ADB device management",
    "demo（模拟板卡）":"demo (simulated board)","--sim 自动注册的演示设备，可自由增删":"Demo device registered by --sim; safe to edit or delete",
    "暂无设备，请先在设备管理页添加":"No devices. Add one on the Devices page.","暂无会话，点「新建会话」开始":"No sessions. Select New session to begin.",
    "输入命令，回车执行（↑↓ 历史）":"Enter a command; press Enter to run (↑↓ history)","输入问题，Enter 发送（Shift+Enter 换行）":"Enter a question; Enter to send (Shift+Enter for a new line)",
    "只读枚举（各面板结果缓存 10s，无任何写操作）":"Read-only inventory (results cached for 10s; no writes)"
  };
  const phrases = [["请求失败","Request failed"],["保存失败","Save failed"],["删除失败","Delete failed"],["连接失败","Connection failed"],
    ["上位机","Host"],["配置","Config"],["sshpass 无","sshpass unavailable"],["sshpass 有","sshpass available"],["adb 无","adb unavailable"],["adb 有","adb available"],
    ["模拟板卡","simulated board"],["自动注册的演示设备，可自由增删","demo device registered automatically; safe to edit or delete"],
    ["未发现","No"],["暂无","No"],["确认删除","Delete"],["错误","Error"],["成功","Succeeded"],["正在","In progress: "],
    ["工具调用","tool call(s)"],["工具结果","tool result"],["输出已截断","output truncated"],["截断","truncated"],
    ["总数 ","Total "],["探测到 ","Detected "],[" 个日志源"," log source(s)"],["无法读取内核日志","Unable to read kernel logs"],
    ["无权限","permission denied"],["不可读","not readable"],["采集失败","Collection failed"],["三级降级均不可用","all three fallbacks are unavailable"],
    ["未安装","not installed"],["无法读取","Unable to read"],["串口节点","serial ports"],["视频设备","video devices"],["总线或设备","buses or devices"],
    ["控制器","controllers"],["看门狗","watchdogs"],["电源调节器","regulators"]];
  const originals = new WeakMap();
  const attrs = ["placeholder", "title", "aria-label"];
  let language = localStorage.getItem(KEY) === "en" ? "en" : "zh";
  function tr(value) {
    const part = value.trim();
    if (words[part]) return value.replace(part, words[part]);
    return phrases.reduce((text, pair) => text.split(pair[0]).join(pair[1]), value);
  }
  function text(node) {
    const current = node.nodeValue;
    let source = originals.get(node);
    if (source === undefined || (current !== source && current !== tr(source))) originals.set(node, source = current);
    const desired = language === "en" ? tr(source) : source;
    if (node.nodeValue !== desired) node.nodeValue = desired;
  }
  function node(item) {
    if (item.nodeType === Node.TEXT_NODE) return text(item);
    if (item.nodeType !== Node.ELEMENT_NODE || /^(SCRIPT|STYLE|CODE|PRE)$/.test(item.tagName)) return;
    attrs.forEach((name) => {
      if (!item.hasAttribute(name)) return;
      const key = "i18n" + name.replace("-", "");
      const current = item.getAttribute(name);
      let source = item.dataset[key];
      if (source === undefined || (current !== source && current !== tr(source))) item.dataset[key] = source = current;
      const desired = language === "en" ? tr(source) : source;
      if (current !== desired) item.setAttribute(name, desired);
    });
    Array.from(item.childNodes).forEach(node);
  }
  function refresh() {
    document.documentElement.lang = language === "en" ? "en" : "zh-CN";
    document.title = language === "en" ? words["RK 设备运维控制台"] : "RK 设备运维控制台";
    node(document.body);
    const button = document.getElementById("language-toggle");
    if (button) button.textContent = language === "en" ? "中文" : "English";
  }
  function setLanguage(next) {
    language = next === "en" ? "en" : "zh";
    localStorage.setItem(KEY, language); refresh();
    window.dispatchEvent(new CustomEvent("rkss:languagechange", { detail: { language } }));
  }
  ["alert", "confirm", "prompt"].forEach((name) => {
    const native = window[name].bind(window);
    window[name] = function (message, defaultValue) {
      const localized = language === "en" ? tr(String(message)) : message;
      return name === "prompt" ? native(localized, defaultValue) : native(localized);
    };
  });
  document.addEventListener("DOMContentLoaded", function () {
    refresh();
    document.getElementById("language-toggle").addEventListener("click", () => setLanguage(language === "zh" ? "en" : "zh"));
    new MutationObserver((records) => records.forEach((record) => {
      if (record.type === "characterData") text(record.target);
      record.addedNodes.forEach(node);
    })).observe(document.body, { childList: true, subtree: true, characterData: true });
  });
  window.RKS = window.RKS || {};
  window.RKS.i18n = { get language() { return language; }, setLanguage, translate: tr };
}());
