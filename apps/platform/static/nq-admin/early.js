// Runs in <head> before first paint, so a collapsed sidebar never flashes open.
try {
  if (localStorage.getItem("nq.sidebar") === "collapsed") document.documentElement.dataset.nqSidebar = "collapsed";
} catch {
  // Storage blocked: start expanded.
}
