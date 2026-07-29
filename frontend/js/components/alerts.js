// Toasts: éxito, error de fuente (regla S3: degradarse con gracia, nunca
// pantalla rota), auto-dismiss 5s.
const CONTAINER_ID = "toast-container";

function getContainer() {
  let el = document.getElementById(CONTAINER_ID);
  if (!el) {
    el = document.createElement("div");
    el.id = CONTAINER_ID;
    el.className = "toast-container";
    document.body.appendChild(el);
  }
  return el;
}

export function showToast(message, type = "info") {
  const container = getContainer();
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}
