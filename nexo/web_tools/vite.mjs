// One pinned toolchain resolver for trusted dev and release runners.
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {readFile} from 'node:fs/promises';
export async function loadVite(root){
  const [major,minor]=process.versions.node.split('.').map(Number);
  if(!(major===22&&minor>=12||major>=24))throw new Error('NW4001: Node 22.12 or 24+ is required.');
  let file,packageFile;
  for(const base of [join(root,'package.json'),new URL('./package.json',import.meta.url)]){
    try{const require=createRequire(base);file=require.resolve('vite');packageFile=require.resolve('vite/package.json');break;}catch{}
  }
  if(!file)throw new Error('NW4001: Pinned Vite is unavailable. Run npm ci in the generated project or npm ci --prefix nexo/web_tools.');
  const pkg=JSON.parse(await readFile(packageFile,'utf8'));
  if(pkg.version!=='8.3.5')throw new Error('NW4001: Expected pinned Vite 8.3.5; run npm ci.');
  return {vite:await import(pathToFileURL(file)),packageFile};
}
