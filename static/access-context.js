/* 公网共享时，明确数据所在主机，并把 DICOM 导入切换为浏览器上传。 */
"use strict";

(function () {
  const cloud = window.WindcatcherCloudAccess === true;
  const local = !cloud && ["localhost", "127.0.0.1", "[::1]", "::1"].includes(window.location.hostname);
  window.WindcatcherSharedAccess = !local;
  const activityEnabled = local || window.WindcatcherActivityAccess === true;
  window.WindcatcherActivityEnabled = activityEnabled;
  const activityNav = document.querySelector('[data-route="activity"]');
  if (activityNav) activityNav.hidden = !activityEnabled;
  const activityPage = document.getElementById("page-activity");
  if (activityPage) activityPage.hidden = !activityEnabled;
  if (local) return;

  const badge = document.querySelector(".local-led");
  if (badge) badge.textContent = cloud
    ? (window.WindcatcherDeployment === "public" ? "本机公网 · HTTPS" : "魔搭云端 · HTTPS")
    : "熟人共享 · HTTPS";
  if (cloud) {
    const sample = document.querySelector('[data-route="sample"]');
    if (sample) {
      sample.hidden = false;
      const label = sample.querySelector?.('.label-text');
      if (label) label.textContent = '参考病例';
    }
    const samplePage = document.getElementById("page-sample");
    if (samplePage) samplePage.hidden = false;
  }
  const description = document.querySelector("#page-cases .page-heading p");
  if (description) description.textContent = window.WindcatcherMultiuser === true
    ? "显示当前账号获授权的病例；可上传已授权、已去标识的 DICOM。"
    : "共享服务主机上的病例；可上传已去标识 DICOM，所有试用者操作同一批数据。";
  const footer = document.querySelector("#viewer-card .viewer-foot");
  if (footer) footer.textContent = "服务主机计算 · DICOM 通过 HTTPS 上传后处理";
  const help = document.getElementById("source-help");
  if (help) help.textContent = "请选择一套已去标识的 DICOM 序列文件夹；文件会通过 HTTPS 上传到服务主机处理。不要上传未授权或含患者身份信息的影像。";
  const picker = document.getElementById("btn-pick-directory");
  if (picker) {
    picker.disabled = false;
    picker.textContent = "选择 DICOM 文件夹并上传";
    picker.title = "从当前电脑选择一套 DICOM 序列，提交时上传到服务主机";
  }
  const path = document.getElementById("newcase-path");
  if (path) {
    path.hidden = true;
    path.value = "";
    path.placeholder = "公网模式使用浏览器上传";
  }
  const editPath = document.getElementById("btn-edit-path");
  if (editPath) editPath.hidden = true;
  const upload = document.getElementById("newcase-upload");
  if (upload) upload.removeAttribute("aria-hidden");
  const monitor = document.querySelector(".activity-monitor");
  if (monitor) monitor.hidden = !activityEnabled;
})();
