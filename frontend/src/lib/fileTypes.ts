// File types the upload accepts. Keep in step with backend/app/ingestion/local_reader.py (accepted_types).
// Images are accepted by the backend only while the local OCR models are installed and SCALE_DOCPARSE_MODE is not "off";
// otherwise it answers with a clear message.
export const SUPPORTED_EXTENSIONS: ReadonlySet<string> = new Set([
  "pdf", "docx", "csv", "xlsx", "eml", "txt", "json", "xer", "py", "md",
  "adoc", "bash", "bat", "c", "cc", "cfg", "cmd", "conf", "cpp", "cs",
  "css", "doc", "docm", "dot", "dotx", "env", "go", "gradle", "h", "hpp",
  "htm", "html", "ini", "java", "js", "jsx", "kt", "less", "log", "lua",
  "markdown", "mjs", "odp", "ods", "odt", "php", "pl", "pot", "potx", "pps",
  "ppsx", "ppt", "pptm", "pptx", "properties", "ps1", "r", "rb", "rs", "rst",
  "rtf", "scala", "scss", "sh", "sql", "svelte", "swift", "tex", "toml", "ts",
  "tsv", "tsx", "vue", "xls", "xlsm", "xlt", "xltx", "xml", "yaml", "yml",
  "bmp", "jpeg", "jpg", "png", "tif", "tiff", "webp",
]);

export const ACCEPT_ATTRIBUTE = Array.from(SUPPORTED_EXTENSIONS).map((e) => "." + e).join(",");

export const extensionOf = (name: string): string =>
  name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
