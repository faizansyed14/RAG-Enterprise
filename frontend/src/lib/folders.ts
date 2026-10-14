/** Virtual folder id for documents with no folder_ids. Not a real API folder. */
export const UNFILED_FOLDER_ID = "__unfiled__";

export function isUnfiledFolder(folderId: string | null | undefined): boolean {
  return folderId === UNFILED_FOLDER_ID;
}
