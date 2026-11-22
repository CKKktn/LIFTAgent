// File types supported by this application's document parsers.
const extensions=new Set('csv doc docx htm html jpeg jpg json log md odp ods odt pdf png ppt pptx rtf tsv txt xls xlsx xml yaml yml'.split(' '));
export function uploadFileName(file){return file.webkitRelativePath||file.name;}
export function uploadSkipReason(file){
  const name=uploadFileName(file),parts=name.split(/[\\/]/),base=parts.at(-1);
  if(parts.includes('__MACOSX')||/^(?:~\$|\.~lock\.|\._)|^(?:\.DS_Store|Thumbs\.db)$/i.test(base))return '临时文件或系统文件';
  if(!extensions.has(base.split('.').pop().toLowerCase())||!base.includes('.'))return '本系统暂不支持此格式（图片支持 PNG、JPG、JPEG）';
  if(!file.size)return '空文件';
  return '';
}

export async function collectDocumentIds(loadPage,isCurrent=()=>true){
  const ids=new Set();
  for(let page=1;isCurrent();page++){
    const result=await loadPage(page);
    if(!isCurrent())return null;
    const before=ids.size;
    for(const doc of result.docs||[])if(doc.id)ids.add(doc.id);
    if(ids.size>=Number(result.total||0))return ids;
    if(ids.size===before)throw Error('文件列表已变化，请刷新后重新全选');
  }
  return null;
}
