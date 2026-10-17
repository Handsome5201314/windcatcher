/* 公网共享时，明确数据所在主机，并把 DICOM 导入切换为浏览器上传。 */
"use strict";

(function () {
  const local = ["localhost", "127.0.0.1", "[::1]", "::1"].includes(window.location.hostname);
  window.WindcatcherSharedAccess = !local;
  if (local) return;

  const badge = document.querySelector(".local-led");
  if (badge) badge.textContent = "熟人共享 · HTTPS";
  const description = document.querySelector("#page-cases .page-heading p");
  if (description) description.textContent = "共享服务主机上的病例；可上传已去标识 DICOM，所有试用者操作同一批数据。";
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
  const monitor = document.getElementById("monitor-title")?.closest(".card");
  if (monitor) monitor.hidden = true;
})();
