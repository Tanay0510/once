// once for VS Code and Cursor. Plain JavaScript, no build step, no dependencies.
// - a status bar item with the same line as the Claude Code status line, refreshed every 20 seconds
// - a sidebar that shows the local dashboard (started in the background if needed)
// - commands to open the dashboard, review suggestions in a terminal, and show the last session
const vscode = require("vscode");
const cp = require("child_process");

function cfg(key) { return vscode.workspace.getConfiguration("once").get(key); }
function cwd() {
  const f = vscode.workspace.workspaceFolders;
  return f && f.length ? f[0].uri.fsPath : undefined;
}
function run(args, input) {
  return new Promise((resolve) => {
    const p = cp.spawn(cfg("command") || "once", args, { cwd: cwd(), shell: process.platform === "win32" });
    let out = "";
    p.stdout.on("data", (d) => (out += d));
    p.on("error", () => resolve(""));
    p.on("close", () => resolve(out.trim()));
    if (input !== undefined) { p.stdin.write(input); }
    p.stdin.end();
  });
}

async function dashboardUrl(path) {
  await run(["ui", "--detach", "--no-open", "--port", String(cfg("port") || 4317)]);
  const local = vscode.Uri.parse(`http://127.0.0.1:${cfg("port") || 4317}${path || "/live"}`);
  return (await vscode.env.asExternalUri(local)).toString(true);
}

function frameHtml(url) {
  const origin = new URL(url).origin;
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${origin}; style-src 'unsafe-inline';">
<style>html,body,iframe{margin:0;padding:0;border:0;width:100%;height:100vh;background:transparent}</style></head>
<body><iframe src="${url}" title="once dashboard"></iframe></body></html>`;
}

class Sidebar {
  resolveWebviewView(view) {
    view.webview.options = { enableScripts: true };
    dashboardUrl("/live").then((u) => (view.webview.html = frameHtml(u)));
  }
}

function activate(context) {
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  item.command = "once.openDashboard";
  item.tooltip = "once: open the dashboard";
  const refresh = async () => {
    const line = await run(["statusline"], JSON.stringify({ cwd: cwd() || "" }));
    if (line) { item.text = `$(pulse) ${line}`; item.show(); } else { item.hide(); }
  };
  refresh();
  const timer = setInterval(refresh, 20000);
  context.subscriptions.push(item, { dispose: () => clearInterval(timer) });

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("once.sidebar", new Sidebar()),
    vscode.commands.registerCommand("once.openDashboard", async () => {
      const panel = vscode.window.createWebviewPanel("once.dashboard", "once", vscode.ViewColumn.Beside,
        { enableScripts: true, retainContextWhenHidden: true });
      panel.webview.html = frameHtml(await dashboardUrl("/live"));
    }),
    vscode.commands.registerCommand("once.review", () => {
      const t = vscode.window.createTerminal({ name: "once review", cwd: cwd() });
      t.show(); t.sendText(`${cfg("command") || "once"} review`);
    }),
    vscode.commands.registerCommand("once.last", () => {
      const t = vscode.window.createTerminal({ name: "once last", cwd: cwd() });
      t.show(); t.sendText(`${cfg("command") || "once"} last`);
    })
  );
}

function deactivate() {}
module.exports = { activate, deactivate };
