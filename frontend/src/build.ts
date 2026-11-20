/** The script this page runs, as its own file names it: "/assets/index-<hash>.js" in a built interface. Null on a
 * page that was not built (the development server). The server says which script the build has now: when the two
 * differ, the interface was built again after this page was opened. */
export function ownScript(): string | null {
  return document.querySelector('script[type="module"][src^="/assets/"]')?.getAttribute('src') ?? null
}

export function reload(): void {
  window.location.reload()
}
