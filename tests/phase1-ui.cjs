/* Isolated browser regression for the local development application.
 * Requires Node + Playwright and a running Docker stack with video_tests built.
 * Never uses the user's browser profile or existing accounts/projects.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { randomUUID } = require("node:crypto");
const { execFileSync } = require("node:child_process");
const { chromium, request } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const BASE = "https://localhost:8080";
const DOCKER = process.env.PICTURAS_DOCKER || "docker";
const PASSWORD = "Fase1Browser!2026";
const id = randomUUID();
const media = `/data/phase1-browser-${id}.mp4`;
const temporaryUsers = [];
const temporaryProjects = [];
let browser, context, api, fixture;

function docker(...args) {
  return execFileSync(DOCKER, args, { cwd: ROOT, timeout: 120000, stdio: ["ignore", "pipe", "pipe"] });
}

async function authenticated(method, route, token, body, headers = {}) {
  const response = await api.fetch(route, { method, data: body,
    headers: { Authorization: `Bearer ${token}`, ...headers } });
  return response;
}

async function cleanupAccount(user) {
  const listing = await authenticated("GET", `/api-gateway/projects/${user.user._id}`, user.jwt);
  if (!listing.ok()) throw new Error(`Could not list own fixture projects: ${listing.status()}`);
  for (const project of await listing.json()) {
    temporaryProjects.push(project._id);
    const base = `/api-gateway/projects/${user.user._id}/${project._id}`;
    for (let attempt = 0; attempt < 80; attempt++) {
      const response = await authenticated("GET", base + "/videos", user.jwt);
      if (response.status() === 403) break; // Anonymous image-only fixture.
      assert.equal(response.status(), 200);
      const library = await response.json();
      if (!library.assets.length) break;
      for (const job of library.jobs.filter((j) => ["queued", "processing"].includes(j.state))) {
        await authenticated("POST", base + `/videos/jobs/${job._id}/cancel`, user.jwt);
      }
      const leaf = library.assets.find((candidate) => !candidate.active_job && !library.assets.some(
        (other) => other._id !== candidate._id && (other.source_id === candidate._id || other.input_ids?.includes(candidate._id))));
      if (leaf) {
        assert.equal((await authenticated("DELETE", base + "/videos/" + leaf._id, user.jwt)).status(), 204);
      } else {
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }
    const current = await authenticated("GET", base, user.jwt);
    assert.equal(current.status(), 200);
    const info = await current.json();
    // Let account cleanup use the existing bulk image-resource cleanup for
    // mixed/ZIP fixtures instead of deleting only their project record.
    if (!info.imgs.length) assert.equal((await authenticated("DELETE", base, user.jwt, undefined, { "X-Project-Version": String(info.version) })).status(), 204);
  }
  assert.equal((await authenticated("DELETE", `/api-gateway/users/${user.user._id}`, user.jwt)).status(), 204);
}

async function main() {
  fixture = await fs.mkdtemp(path.join(os.tmpdir(), "picturas-phase1-"));
  const screenshots = process.env.PICTURAS_SCREENSHOTS || fixture;
  await fs.mkdir(screenshots, { recursive: true });
  // The self-signed certificate exception is confined to this local test
  // context. No system trust settings or personal browser profiles are changed.
  api = await request.newContext({ baseURL: BASE, ignoreHTTPSErrors: true });
  const pageErrors = [];
  try {
    const source = [
      "import subprocess, zipfile", "from pathlib import Path",
      `wav=Path('/data/phase1-browser-${id}.wav')`,
      "subprocess.run(['espeak-ng','-v','pt','-s','140','-w',str(wav),'Olá a todos. Este é um teste de legendas automáticas em português. Vamos rever o texto e guardar as legendas.'],check=True)",
      `subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=30','-i',str(wav),'-shortest','-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart','${media}'],check=True)`,
      "wav.unlink()",
      `png=Path('/data/phase1-browser-${id}.png')`,
      "subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=red:s=16x16','-frames:v','1','-threads','1',str(png)],check=True)",
      `with zipfile.ZipFile('/data/phase1-browser-${id}.zip','w') as archive: archive.write(png,'imagem.png')`,
      `blue=Path('/data/phase1-browser-${id}.blue.png')`,
      "subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=16x16','-frames:v','1','-threads','1',str(blue)],check=True)",
      `with zipfile.ZipFile('/data/phase1-browser-${id}.lote.zip','w') as archive: archive.write(png,'imagem.do.zip.png')`,
      `subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=160x90:r=25:d=2','-c:v','libx264','-threads','2','-pix_fmt','yuv420p','/data/phase1-browser-${id}.alt.mp4'],check=True)`,
      `subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=green:s=160x90:r=25:d=2','-c:v','libx264','-threads','2','-pix_fmt','yuv420p','/data/phase1-browser-${id}.other.mp4'],check=True)`,
    ].join("\n");
    docker("compose", "-f", "docker-compose.yaml", "-f", "compose.video-tests.yaml", "run", "--rm", "--no-deps", "--entrypoint", "python", "video_tests", "-c", source);
    const sample = path.join(fixture, "fala-portuguesa.mp4");
    docker("compose", "cp", `video_service:${media}`, sample);
    const image = path.join(fixture, "imagem.png"), zip = path.join(fixture, "imagens.zip");
    docker("compose", "cp", `video_service:/data/phase1-browser-${id}.png`, image);
    docker("compose", "cp", `video_service:/data/phase1-browser-${id}.zip`, zip);
    const blueImage = path.join(fixture, "azul.png"), batchZip = path.join(fixture, "lote.zip");
    const alternate = path.join(fixture, "alternativo.mp4"), other = path.join(fixture, "outro.mp4");
    for (const [suffix, target] of [[".blue.png", blueImage], [".lote.zip", batchZip], [".alt.mp4", alternate], [".other.mp4", other]]) {
      docker("compose", "cp", `video_service:/data/phase1-browser-${id}${suffix}`, target);
    }
    const email = `phase1-browser-${id}@picturas.local`;
    for (const data of [{ name: "PHASE1 BROWSER FIXTURE", type: "anonymous" },
      { name: "PHASE1 BROWSER FIXTURE", email, password: PASSWORD, type: "premium" }]) {
      const response = await api.post("/api-gateway/users/", { data });
      assert.equal(response.status(), 201);
      temporaryUsers.push(await response.json());
    }
    const user = temporaryUsers[1];
    browser = await chromium.launch({ headless: true, executablePath: process.env.PICTURAS_BROWSER || undefined });
    context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 1100 }, acceptDownloads: true });
    await context.addInitScript((session) => {
      if (!localStorage.getItem("session")) localStorage.setItem("session", JSON.stringify(session));
    }, { user: temporaryUsers[0].user, token: temporaryUsers[0].jwt });
    // Do not fetch background pictures or external services in this regression.
    await context.route("**/*", (route) => route.request().url().startsWith(BASE) || route.request().url().startsWith("blob:")
      ? route.continue() : route.abort());
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    page.on("pageerror", (error) => pageErrors.push(error.message));
    async function dropFiles(label, entries, target) {
      const payload = [];
      for (const entry of entries) payload.push({ name: entry.name || path.basename(entry.file), type: entry.type ?? (entry.file.endsWith(".mp4") ? "video/mp4" : entry.file.endsWith(".zip") ? "application/zip" : "image/png"), bytes: Array.from(await fs.readFile(entry.file)) });
      const transfer = await page.evaluateHandle((items) => {
        const data = new DataTransfer();
        for (const item of items) data.items.add(new File([new Uint8Array(item.bytes)], item.name, { type: item.type }));
        return data;
      }, payload);
      await (target || page.getByLabel(label, { exact: true }).locator("..")).dispatchEvent("drop", { dataTransfer: transfer });
      await transfer.dispose();
      // Wait for asynchronous ZIP reading/deduplication, not only the drop event.
      await page.waitForFunction((name) => document.querySelector(`input[aria-label="${name}"]`)?.closest('[aria-busy]')?.getAttribute('aria-busy') !== 'true', label);
    }
    async function openTool(name) {
      const button = page.getByRole("button", { name: `Ferramenta: ${name}`, exact: true });
      if (await button.getAttribute("aria-pressed") !== "true") await button.click();
    }
    async function openHistory() {
      await page.getByRole("button", { name: "Processamentos", exact: true }).click();
      await page.getByRole("dialog", { name: /^Processamentos/ }).waitFor();
    }
    async function closeDialog() {
      await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).click();
      await page.getByRole("dialog").waitFor({ state: "hidden" });
    }
    await page.goto(BASE + "/login");
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Login", exact: true }).click();
    await page.waitForURL((url) => url.pathname === "/dashboard");
    console.log("UI: login real passou.");

    await page.getByRole("button", { name: "New Project", exact: true }).click();
    await page.getByLabel("Project Name").fill("Vídeo sem imagens — teste Fase 1");
    assert.equal(await page.getByRole("button", { name: "Create", exact: true }).isEnabled(), true);
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.waitForURL((url) => /^\/dashboard\/[^/]+\/video$/.test(url.pathname));
    const emptyPid = new URL(page.url()).pathname.split("/")[2];
    await page.getByText("Ainda não existem vídeos.", { exact: false }).waitFor();
    const project = await (await authenticated("GET", `/api-gateway/projects/${user.user._id}/${emptyPid}`, user.jwt)).json();
    assert.deepEqual(project.imgs, []);
    console.log("UI: botão lateral continua a criar projeto vazio para vídeo, sem imagens.");

    // Exercise the actual dashboard entry point that the old test omitted.
    await page.goto(BASE + "/dashboard");
    const centralInput = page.getByLabel("Ficheiros para novo projeto", { exact: true });
    const chooseCentralFile = async () => {
      const chooser = page.waitForEvent("filechooser");
      await centralInput.locator("..").click();
      await (await chooser).setFiles(sample);
    };
    await chooseCentralFile();
    await page.getByRole("dialog").waitFor();
    await page.getByRole("dialog").getByText("fala-portuguesa.mp4", { exact: true }).waitFor();
    await page.keyboard.press("Escape");
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    await chooseCentralFile();
    await page.getByLabel("Project Name").fill("Vídeo pela caixa central — sem imagens");
    await page.screenshot({ path: path.join(screenshots, "dashboard-video-selecionado.png"), animations: "disabled" });
    const projectsUrl = `/api-gateway/projects/${user.user._id}`;
    const before = (await (await authenticated("GET", projectsUrl, user.jwt)).json()).length;
    let interrupted = false;
    await page.route("**/api-gateway/projects/*/*/videos", async (route) => {
      if (!interrupted && route.request().method() === "POST") {
        interrupted = true;
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Falha simulada no carregamento de teste." }) });
      } else await route.continue();
    });
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.getByRole("button", { name: "Retomar carregamento", exact: true }).waitFor();
    assert(interrupted);
    assert.equal((await (await authenticated("GET", projectsUrl, user.jwt)).json()).length, before + 1);
    await page.getByRole("button", { name: "Retomar carregamento", exact: true }).click();
    await page.waitForURL((url) => /^\/dashboard\/[^/]+\/video$/.test(url.pathname));
    const pid = new URL(page.url()).pathname.split("/")[2];
    const base = `/api-gateway/projects/${user.user._id}/${pid}/videos`;
    assert.equal((await (await authenticated("GET", projectsUrl, user.jwt)).json()).length, before + 1);
    assert.deepEqual((await (await authenticated("GET", `${projectsUrl}/${pid}`, user.jwt)).json()).imgs, []);
    await page.getByRole("heading", { name: "fala-portuguesa.mp4 · Original", exact: true }).waitFor();
    console.log("UI: MP4 pela caixa central, cancelamento/reseleção e upload automático sem imagens passaram; falha simulada retoma no mesmo projeto.");

    let queries = 0;
    page.on("request", (req) => { if (req.method() === "GET" && new URL(req.url()).pathname === base) queries++; });
    await page.waitForTimeout(1000);
    const idleQueries = queries;
    await page.waitForTimeout(4200);
    assert.equal(queries, idleQueries, "Idle page kept polling the library");
    let library = await (await authenticated("GET", base, user.jwt)).json();
    const original = library.assets[0];
    assert.equal(library.assets.length, 1);
    await page.getByRole("button", { name: "Adicionar vídeo", exact: true }).click();
    await page.getByLabel("Vídeos a carregar", { exact: true }).setInputFiles(sample);
    await page.getByText("Este ficheiro já estava no projeto.", { exact: false }).waitFor();
    assert.equal((await (await authenticated("GET", base, user.jwt)).json()).assets.length, 1);
    console.log("UI: carregamento, indicação visual e reutilização do ficheiro passaram; sem polling em repouso.");

    await page.getByRole("button", { name: "2×", exact: true }).click();
    await page.getByRole("button", { name: "Aplicar velocidade", exact: true }).click();
    await openHistory();
    await page.getByRole("button", { name: "Abrir vídeo resultante", exact: true }).waitFor();
    await closeDialog();
    await page.getByRole("button", { name: "Aplicar velocidade", exact: true }).click();
    await page.getByText("Este pedido já tinha um resultado.", { exact: false }).waitFor();
    library = await (await authenticated("GET", base, user.jwt)).json();
    assert.equal(library.jobs.length, 1);
    const result = library.assets.find((a) => a.kind === "speed");
    assert(result);
    await openHistory();
    await page.getByRole("button", { name: "Abrir vídeo resultante", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("video")?.readyState >= 1);
    const playerDuration = await page.locator("video").evaluate((video) => video.duration);
    assert(Math.abs(playerDuration - original.metadata.duration / 2) <= .1);
    await page.locator("video").evaluate(async (video) => { video.muted = true; await video.play(); });
    await page.waitForFunction(() => document.querySelector("video")?.currentTime > .3);
    await page.locator("video").evaluate((video) => video.pause());
    const videoDownload = page.waitForEvent("download");
    await page.getByRole("button", { name: "Descarregar esta versão MP4", exact: true }).click();
    await (await videoDownload).saveAs(path.join(fixture, "resultado.mp4"));
    assert((await fs.stat(path.join(fixture, "resultado.mp4"))).size > 0);
    console.log("UI: velocidade, pedido repetido, abertura, reprodução e download do resultado passaram.");

    await openTool("Miniatura");
    await page.getByRole("button", { name: "Usar instante do leitor", exact: true }).click();
    await page.getByRole("button", { name: "Pré-visualizar miniatura", exact: true }).click();
    await page.getByRole("img", { name: "Nova miniatura proposta", exact: true }).waitFor();
    await page.getByRole("button", { name: "Guardar miniatura", exact: true }).click();
    await page.getByRole("img", { name: "Miniatura atual do vídeo", exact: true }).waitFor();
    console.log("UI: miniatura pré-visualizada e guardada.");

    await page.getByLabel("Vídeo / versão", { exact: true }).selectOption(original._id);
    await openTool("Legendas");
    await page.getByRole("button", { name: "Gerar rascunho de legendas", exact: true }).click();
    await page.getByLabel("Texto da legenda 1", { exact: true }).waitFor({ timeout: 120000 });
    await page.waitForFunction(() => !document.querySelector('textarea[aria-label="Texto da legenda 1"]')?.disabled);
    await page.getByLabel("Texto da legenda 1", { exact: true }).fill("Legendas revistas no teste da Fase 1.");
    await page.getByRole("button", { name: "Pré-visualizar estas legendas", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("video")?.textTracks[0]?.cues?.length > 0);
    await page.getByRole("button", { name: "Guardar legendas revistas", exact: true }).click();
    await page.getByRole("button", { name: "Descarregar VTT", exact: true }).waitFor();
    for (const format of ["SRT", "VTT"]) {
      const download = page.waitForEvent("download");
      await page.getByRole("button", { name: `Descarregar ${format}`, exact: true }).click();
      const file = path.join(fixture, `legendas.${format.toLowerCase()}`);
      await (await download).saveAs(file);
      const text = await fs.readFile(file, "utf8");
      assert(text.includes("Legendas revistas no teste da Fase 1."));
      if (format === "VTT") assert(text.startsWith("WEBVTT\n"));
    }
    console.log("UI: transcrição real, revisão, pistas no leitor e downloads SRT/VTT passaram.");
    await page.screenshot({ path: path.join(screenshots, "fase1-legendas.png"), fullPage: true });
    await page.getByRole("heading", { name: "Legendas automáticas", exact: true }).locator("..").screenshot({ path: path.join(screenshots, "fase1-legendas-painel.png") });
    await page.getByRole("heading", { name: "Vídeos do projeto", exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(screenshots, "fase1-biblioteca.png") });
    await openHistory();
    await page.screenshot({ path: path.join(screenshots, "fase1-processamentos.png") });
    await closeDialog();

    assert.equal(await page.getByRole("button", { name: "Apagar versão", exact: true }).isDisabled(), true);
    await page.getByLabel("Vídeo / versão", { exact: true }).selectOption(result._id);
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Apagar versão", exact: true }).click();
    await openHistory();
    await page.getByText("O vídeo resultante foi removido.", { exact: true }).waitFor();
    library = await (await authenticated("GET", base, user.jwt)).json();
    assert.equal(library.assets.length, 1);
    assert.equal(library.assets[0]._id, original._id);
    await page.screenshot({ path: path.join(screenshots, "fase1-versao-removida.png"), fullPage: true });
    await closeDialog();

    await openTool("Cor e filtros");
    await page.getByLabel("Valor do filtro 1", { exact: true }).fill("25");
    await openTool("Velocidade");
    await openTool("Cor e filtros");
    assert.equal(await page.getByLabel("Valor do filtro 1", { exact: true }).inputValue(), "25", "Changing tools discarded the filter draft");
    await page.getByRole("button", { name: "Pré-visualizar fotograma", exact: true }).click();
    await page.getByRole("img", { name: "Pré-visualização dos filtros pela ordem escolhida", exact: true }).waitFor();
    await page.getByRole("button", { name: "Aplicar ao vídeo", exact: true }).click();
    await openHistory();
    await page.getByRole("button", { name: "Abrir vídeo resultante", exact: true }).waitFor();
    library = await (await authenticated("GET", base, user.jwt)).json();
    const color = library.assets.find((a) => a.kind === "color");
    assert(color);
    await page.getByRole("button", { name: "Abrir vídeo resultante", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("video")?.readyState >= 1);
    assert(Math.abs(await page.locator("video").evaluate((v) => v.duration) - original.metadata.duration) <= .1);
    await page.getByLabel("Vídeo / versão", { exact: true }).selectOption(original._id);
    await openTool("Transições");
    await page.getByLabel("Segundo clip", { exact: true }).selectOption(color._id);
    await page.getByRole("button", { name: "Criar vídeo com transição", exact: true }).click();
    await openHistory();
    await page.waitForFunction(() => [...document.querySelectorAll("h3")].some((h) => h.textContent.includes("Transição") && h.parentElement.textContent.includes("Concluído")));
    library = await (await authenticated("GET", base, user.jwt)).json();
    const transition = library.assets.find((a) => a.kind === "transition");
    assert(transition);
    await closeDialog();
    await page.getByLabel("Vídeo / versão", { exact: true }).selectOption(transition._id);
    await page.waitForFunction(() => document.querySelector("video")?.readyState >= 1);
    const composedDuration = await page.locator("video").evaluate((v) => v.duration);
    assert(Math.abs(composedDuration - (original.metadata.duration * 2 - .5)) <= .1);
    await page.screenshot({ path: path.join(screenshots, "fase1-transicao.png"), fullPage: true });
    assert(await page.getByRole("button", { name: `Selecionar versão: ${original.name} · Original`, exact: true }).getAttribute("aria-pressed") === "false");
    await page.getByRole("button", { name: `Selecionar versão: ${original.name} · Original`, exact: true }).click();
    assert.equal(await page.getByLabel("Vídeo / versão", { exact: true }).inputValue(), original._id);
    await page.getByRole("button", { name: "Fechar opções da ferramenta", exact: true }).click();
    assert.equal(await page.getByRole("complementary", { name: "Opções da ferramenta", exact: true }).isVisible(), false);
    await openTool("Velocidade");
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      await page.waitForTimeout(350);
      const geometry = await page.getByLabel("Editor de vídeo", { exact: true }).evaluate((node) => ({ height: node.getBoundingClientRect().height, scroll: node.scrollHeight, width: document.documentElement.scrollWidth, viewport: innerWidth }));
      assert(geometry.scroll <= geometry.height + 2, `Editor needs whole-page scrolling: ${JSON.stringify(geometry)}`);
      assert(geometry.width <= geometry.viewport + 2, `Horizontal page overflow: ${JSON.stringify(geometry)}`);
      const playerBox = await page.locator("video").boundingBox();
      assert(playerBox && playerBox.height >= 90 && playerBox.width >= 200, "Player is not usable at this viewport");
      for (const name of ["Processamentos", "Descarregar esta versão MP4", "Apagar versão"]) {
        const box = await page.getByRole("button", { name, exact: true }).boundingBox();
        assert(box && box.x >= 0 && box.x + box.width <= viewport.width + 1, `Header action clipped: ${name}`);
      }
      await page.screenshot({ path: path.join(screenshots, `hud-${viewport.width}.png`), animations: "disabled" });
    }
    await page.setViewportSize({ width: 1440, height: 1100 });
    console.log("UI: HUD sem scroll global em desktop/telemóvel; ferramentas, rascunhos e escolha direta de versões passaram.");
    console.log("UI: pré-visualização e aplicação de filtros, seleção dos dois clips, transição e reprodução passaram.");

    await page.goto(BASE + "/dashboard");
    const dragData = await page.evaluateHandle(({ bytes }) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array(bytes)], "arrastado.mp4", { type: "video/mp4" }));
      return transfer;
    }, { bytes: Array.from(await fs.readFile(sample)) });
    await page.getByLabel("Ficheiros para novo projeto", { exact: true }).locator("..").dispatchEvent("drop", { dataTransfer: dragData });
    await dragData.dispose();
    await page.getByLabel("Project Name").fill("MP4 arrastado sem imagens");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.waitForURL((url) => /^\/dashboard\/[^/]+\/video$/.test(url.pathname));
    const dragPid = new URL(page.url()).pathname.split("/")[2];
    assert.deepEqual((await (await authenticated("GET", `${projectsUrl}/${dragPid}`, user.jwt)).json()).imgs, []);
    assert.equal((await (await authenticated("GET", `${projectsUrl}/${dragPid}/videos`, user.jwt)).json()).assets.length, 1);
    console.log("UI: arrastar um MP4 diretamente para a caixa central passou, sem imagens.");

    await page.goto(BASE + "/dashboard");
    await page.getByLabel("Ficheiros para novo projeto", { exact: true }).setInputFiles([sample, image]);
    await page.getByLabel("Project Name").fill("Projeto misto de imagem e vídeo");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.waitForURL((url) => /^\/dashboard\/[^/]+\/video$/.test(url.pathname));
    const mixedPid = new URL(page.url()).pathname.split("/")[2];
    assert.equal((await (await authenticated("GET", `${projectsUrl}/${mixedPid}`, user.jwt)).json()).imgs.length, 1);
    assert.equal((await (await authenticated("GET", `${projectsUrl}/${mixedPid}/videos`, user.jwt)).json()).assets.length, 1);
    console.log("UI: seleção mista preserva a imagem e carrega o vídeo no mesmo projeto.");

    await page.goto(BASE + "/dashboard");
    await page.getByLabel("Ficheiros para novo projeto", { exact: true }).setInputFiles(zip);
    await page.getByLabel("Project Name").fill("ZIP de imagens original");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.waitForURL((url) => /^\/dashboard\/[^/]+$/.test(url.pathname));
    const zipPid = new URL(page.url()).pathname.split("/")[2];
    assert.equal((await (await authenticated("GET", `${projectsUrl}/${zipPid}`, user.jwt)).json()).imgs.length, 1);
    await page.getByRole("button", { name: "Vídeos", exact: true }).waitFor();
    console.log("UI: ZIP continua a criar projeto de imagens e abrir o editor original.");

    await page.getByRole("button", { name: "Add Images", exact: true }).click();
    await dropFiles("Imagens a adicionar", [{ file: image, name: "temporaria.png" }]);
    await page.getByRole("button", { name: "Retirar ficheiro temporaria.png", exact: true }).click();
    assert.equal(await page.getByRole("button", { name: "Add", exact: true }).isDisabled(), true);
    await page.keyboard.press("Escape");
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    await page.getByRole("button", { name: "Add Images", exact: true }).click();
    assert.equal(await page.getByRole("button", { name: "Add", exact: true }).isDisabled(), true);
    await dropFiles("Imagens a adicionar", [{ file: sample }]);
    assert.equal(await page.getByRole("button", { name: "Retirar ficheiro fala-portuguesa.mp4", exact: true }).count(), 0);
    await dropFiles("Imagens a adicionar", [{ file: image, name: "invalido.zip", type: "application/zip" }]);
    await page.getByRole("alert").filter({ hasText: "Não foi possível abrir o ZIP invalido.zip." }).waitFor();
    await dropFiles("Imagens a adicionar", [{ file: image, name: "lote.um.png" }]);
    await dropFiles("Imagens a adicionar", [{ file: blueImage, name: "lote.dois.png" }]);
    await dropFiles("Imagens a adicionar", [{ file: batchZip, type: "" }]);
    await page.getByRole("button", { name: "Retirar ficheiro imagem.do.zip.png", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: /^Retirar ficheiro/ }).count(), 3);
    await page.getByRole("button", { name: "Add", exact: true }).click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    assert.equal((await (await authenticated("GET", `${projectsUrl}/${zipPid}`, user.jwt)).json()).imgs.length, 4);
    console.log("UI: Add Images acumula três lotes (incluindo ZIP sem MIME), limpa seleção ao fechar/retirar tudo e mantém a recusa de MP4.");

    await page.getByRole("button", { name: "New Project", exact: true }).click();
    await page.getByLabel("Project Name").fill("Vídeo arrastado dentro da janela aberta");
    await dropFiles("Ficheiros do novo projeto", [{ file: sample }]);
    await page.getByRole("button", { name: "Retirar ficheiro fala-portuguesa.mp4", exact: true }).waitFor();
    await dropFiles("Ficheiros do novo projeto", [{ file: sample }]);
    await dropFiles("Ficheiros do novo projeto", [{ file: image, name: "foto.ferias.2026.png" }]);
    await dropFiles("Ficheiros do novo projeto", [{ file: blueImage, name: "foto.ferias.2026.png" }]);
    await page.getByRole("button", { name: "Retirar ficheiro foto.ferias.2026 (1).png", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Retirar ficheiro fala-portuguesa.mp4", exact: true }).count(), 1);
    assert.equal(await page.getByRole("button", { name: /^Retirar ficheiro/ }).count(), 3);
    await page.screenshot({ path: path.join(screenshots, "janela-arrastamento-misto.png"), animations: "disabled" });
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.waitForURL((url) => /^\/dashboard\/[^/]+\/video$/.test(url.pathname));
    const sidePid = new URL(page.url()).pathname.split("/")[2];
    const sideProject = await (await authenticated("GET", `${projectsUrl}/${sidePid}`, user.jwt)).json();
    assert.equal(sideProject.imgs.length, 2);
    assert(sideProject.imgs.some((item) => item.name === "foto.ferias.2026 (1).png"));
    assert.equal((await (await authenticated("GET", `${projectsUrl}/${sidePid}/videos`, user.jwt)).json()).assets.length, 1);
    console.log("UI: arrastar MP4 e imagens para dentro da janela já aberta acumula todos os lotes, evita repetição e conserva nomes com vários pontos.");

    await dropFiles("Vídeos a carregar", [{ file: alternate }], page.getByRole("region", { name: "Versão selecionada", exact: true }));
    await page.getByText("Vídeo carregado e disponível", { exact: false }).waitFor();
    assert.equal((await (await authenticated("GET", `${projectsUrl}/${sidePid}/videos`, user.jwt)).json()).assets.length, 2);
    await page.getByRole("button", { name: "Adicionar vídeo", exact: true }).click();
    await page.getByLabel("Vídeos a carregar", { exact: true }).setInputFiles([
      { name: "falso.mp4", mimeType: "video/mp4", buffer: Buffer.from("Nao e um video valido.") },
      { name: "outro.mp4", mimeType: "video/mp4", buffer: await fs.readFile(other) },
    ]);
    await page.getByRole("alert").filter({ hasText: "falso.mp4" }).waitFor();
    await closeDialog();
    await page.getByRole("heading", { name: "outro.mp4 · Original", exact: true }).waitFor();
    assert.equal((await (await authenticated("GET", `${projectsUrl}/${sidePid}/videos`, user.jwt)).json()).assets.length, 3);
    console.log("UI: arrastar MP4 diretamente para o leitor passou; janela de carregamento aceita vários MP4 e continua após ficheiro inválido.");

    await page.getByRole("button", { name: "New Project", exact: true }).click();
    await page.getByLabel("Project Name").fill("Só um vídeo arrastado na janela aberta");
    await dropFiles("Ficheiros do novo projeto", [{ file: sample }]);
    await page.getByRole("button", { name: "Retirar ficheiro fala-portuguesa.mp4", exact: true }).waitFor();
    const chooserInDialog = page.waitForEvent("filechooser");
    await page.getByRole("dialog").getByRole("button", { name: "Escolher ficheiros", exact: true }).click();
    await (await chooserInDialog).setFiles(sample);
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.waitForURL((url) => /^\/dashboard\/[^/]+\/video$/.test(url.pathname) && url.pathname !== `/dashboard/${sidePid}/video`);
    const onlyVideoPid = new URL(page.url()).pathname.split("/")[2];
    assert.deepEqual((await (await authenticated("GET", `${projectsUrl}/${onlyVideoPid}`, user.jwt)).json()).imgs, []);
    assert.equal((await (await authenticated("GET", `${projectsUrl}/${onlyVideoPid}/videos`, user.jwt)).json()).assets.length, 1);
    console.log("UI: caso exato da janela aberta com só um MP4 arrastado passou, sem imagens; botão Escolher ficheiros da mesma caixa também passou.");
    assert.deepEqual(pageErrors, [], "Uncaught browser errors");
    console.log("UI: confirmação e eliminação do resultado passaram; original preservado e sem erros JavaScript não tratados.");
  } catch (error) {
    if (pageErrors.length) console.error("Erros JavaScript no navegador:", pageErrors);
    if (context?.pages()[0]) {
      const target = process.env.PICTURAS_SCREENSHOTS || fixture;
      await fs.mkdir(target, { recursive: true });
      await context.pages()[0].screenshot({ path: path.join(target, "fase1-falha.png"), fullPage: true }).catch(() => {});
    }
    throw error;
  } finally {
    await browser?.close();
    const errors = [];
    for (const user of temporaryUsers.reverse()) {
      try { await cleanupAccount(user); } catch (error) { errors.push(error.message); }
    }
    // Project deletion intentionally keeps processing history. Remove only
    // this test's historical records after its own fixture cleanup succeeds.
    if (!errors.length && temporaryProjects.length) {
      try {
        docker("compose", "exec", "-T", "video_service", "python", "-c",
          "import json,sys; from app import jobs; jobs.delete_many({'project_id': {'$in': json.loads(sys.argv[1])}})",
          JSON.stringify(temporaryProjects));
      } catch (error) { errors.push(error.message); }
    }
    try { docker("compose", "exec", "-T", "video_service", "python", "-c", `from pathlib import Path; [Path('/data/phase1-browser-${id}'+ext).unlink(missing_ok=True) for ext in ['.mp4','.wav','.png','.zip','.blue.png','.lote.zip','.alt.mp4','.other.mp4']]`); }
    catch (error) { errors.push(error.message); }
    await api?.dispose();
    if (fixture && !process.env.PICTURAS_SCREENSHOTS) console.log("Artefactos de teste:", fixture);
    assert.equal(errors.length, 0, "Fixture cleanup: " + errors.join("; "));
  }
}

main().catch((error) => { console.error(error.stack); process.exitCode = 1; });
