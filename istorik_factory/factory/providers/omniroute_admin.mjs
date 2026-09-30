// Локальный вызов служебного API OmniRoute тем же способом, что и его собственная команда `omniroute`
// (машинный CLI-токен работает только для адреса на этом компьютере). Ключи в аргументах не передаются.
// Использование: node omniroute_admin.mjs <папка пакета omniroute> <путь /api/...> [GET|POST] [JSON тела]
// Вывод: одна строка «<HTTP-статус> <тело ответа>».
import { pathToFileURL } from "node:url";

const [root, path, method = "GET", body] = process.argv.slice(2);
try {
  const { apiFetch } = await import(pathToFileURL(`${root}/bin/cli/api.mjs`).href);
  const res = await apiFetch(path, {
    method,
    body: body ? JSON.parse(body) : undefined,
    headers: { "content-type": "application/json" },
    retry: false,
  });
  process.stdout.write(`${res.status} ${(await res.text()).replace(/\s*\n\s*/g, " ")}\n`);
} catch (e) {
  process.stdout.write(`0 ${JSON.stringify({ error: String(e && e.message ? e.message : e) })}\n`);
}
