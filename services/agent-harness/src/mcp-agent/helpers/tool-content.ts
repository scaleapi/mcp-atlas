// Chat-Completions tool messages carry text parts only, so replace non-text MCP
// blocks with a one-line descriptor instead of failing the whole tool result.
export function describeNonTextContent(content: any[]): any[] {
  return content.map((item: any) => {
    if (item.type === 'text' || item.image_url) return item;
    if (item.type === 'resource' && typeof item.resource?.text === 'string') {
      return { type: 'text', text: item.resource.text };
    }
    const uri = item.uri || item.resource?.uri;
    const mimeType = item.mimeType || item.resource?.mimeType || 'unknown type';
    return { type: 'text', text: `[${item.type} content omitted: ${mimeType}${uri ? ` ${uri}` : ''}]` };
  });
}
