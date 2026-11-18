// Counted rather than save-and-restore: stacked modals can close in the same
// render, and React cleans up the outer one first, which would leave the
// inner one restoring "hidden" for good.
let holders = 0;

export function lockBodyScroll(): () => void {
  if (holders++ === 0) document.body.style.overflow = "hidden";
  let released = false;
  return () => {
    if (released) return;
    released = true;
    if (--holders === 0) document.body.style.overflow = "";
  };
}
