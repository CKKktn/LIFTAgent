// File types supported by this application's document parsers.
const extensions=new Set('csv doc docx htm html json log md odp ods odt pdf ppt pptx rtf tsv txt xls xlsx xml yaml yml'.split(' '));
export function uploadFileName(file){return file.webkitRelativePath||file.name;}
export function uploadSkipReason(file){
  const name=uploadFileName(file),parts=name.split(/[\\/]/),base=parts.at(-1);
  if(parts.includes('__MACOSX')||/^(?:~\$|\.~lock\.|\._)|^(?:\.DS_Store|Thumbs\.db)$/i.test(base))return '临时文件或系统文件';
  if(!extensions.has(base.split('.').pop().toLowerCase())||!base.includes('.'))return '本系统暂不支持此格式；扫描件和图片请先转换为带文字的 PDF 或 Word';
  if(!file.size)return '空文件';
  return '';
}
