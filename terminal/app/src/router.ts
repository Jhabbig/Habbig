// router.ts — hash router for narve Terminal.
//
// Routes: #/markets (default) · #/sources · #/question/:id · #/ingest
// Each screen lives at ./screens/<name>.ts and exports
//     mount(root: HTMLElement, params: Record<string, string>): void
//
// The dynamic import specifier is a template literal ON PURPOSE:
//  * tsc types a non-literal import() as Promise<any>, so the shell
//    typechecks even when sibling-built screen modules are absent;
//  * Vite's dynamic-import-vars turns `./screens/${name}.ts` into a
//    glob, so every present screen is code-split into the bundle.
// A missing/broken screen module renders a styled "screen not built"
// panel instead of a blank window.

export interface RouteMatch {
  screen: string;
  params: Record<string, string>;
}

type ScreenModule = {
  mount?: (root: HTMLElement, params: Record<string, string>) => void;
};

const KNOWN: readonly string[] = ["markets", "sources", "question", "ingest"];

export function parseHash(hash: string): RouteMatch {
  const clean = hash.replace(/^#\/?/, "");
  const segments = clean.split("/");
  const head = segments[0] ?? "";
  if (head === "question" && segments.length > 1) {
    const id = decodeURIComponent(segments.slice(1).join("/"));
    return { screen: "question", params: { id } };
  }
  if (KNOWN.includes(head)) {
    return { screen: head, params: {} };
  }
  return { screen: "markets", params: {} }; // unknown hash → default screen
}

/* Monotonic mount token: a late-resolving import from a stale
   navigation must never clobber the screen the user is now on. */
let mountSeq = 0;

async function mountScreen(root: HTMLElement, match: RouteMatch): Promise<void> {
  const seq = ++mountSeq;
  try {
    const mod = (await import(`./screens/${match.screen}.ts`)) as ScreenModule;
    if (seq !== mountSeq) return;
    if (typeof mod.mount !== "function") {
      throw new Error(`screens/${match.screen}.ts exports no mount() function`);
    }
    root.innerHTML = "";
    root.scrollTop = 0;
    mod.mount(root, match.params);
  } catch (err) {
    if (seq !== mountSeq) return;
    renderNotBuilt(root, match.screen, err);
  }
}

function renderNotBuilt(root: HTMLElement, screen: string, err: unknown): void {
  const detail = err instanceof Error ? err.message : String(err);
  root.innerHTML = "";

  const panel = document.createElement("section");
  panel.className = "nv-panel nv-panel--pad";

  const marker = document.createElement("div");
  marker.className = "nv-marker";
  marker.textContent = `[ ${screen.toUpperCase()} / -- ]`;

  const title = document.createElement("h2");
  title.className = "nv-notbuilt-title";
  title.textContent = "SCREEN NOT BUILT";

  const copy = document.createElement("p");
  copy.className = "nv-notbuilt-copy";
  copy.textContent =
    `This build of the shell has no ${screen.toUpperCase()} module — ` +
    `src/screens/${screen}.ts failed to load. Rebuild the app with the ` +
    `screen modules present, or navigate to another tab.`;

  const code = document.createElement("code");
  code.className = "nv-notbuilt-detail";
  code.textContent = detail;

  panel.append(marker, title, copy, code);
  root.appendChild(panel);
}

export function startRouter(
  root: HTMLElement,
  onChange?: (match: RouteMatch) => void,
): void {
  const apply = (): void => {
    if (!window.location.hash) {
      window.location.replace("#/markets"); // re-fires hashchange → apply()
      return;
    }
    const match = parseHash(window.location.hash);
    onChange?.(match);
    void mountScreen(root, match);
  };
  window.addEventListener("hashchange", apply);
  apply();
}
