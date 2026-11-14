// cytoscape-dagre ships no types and has no @types package. The plugin's
// only surface here is the default export passed to cytoscape.use().
declare module "cytoscape-dagre" {
  import type { Ext } from "cytoscape";
  const extension: Ext;
  export default extension;
}
