/*
  Prototype local-market resolver.
  Primary: public ZIP -> DMA table.
  Fallback: public ZIP centroids + DMA GeoJSON point-in-polygon.
  Data is loaded only when a viewer sets a ZIP code.
*/
(function(){
  const SOURCES={
    primary:'https://raw.githubusercontent.com/kylezengo/census/refs/heads/main/zip_to_dma.csv',
    centroids:'https://raw.githubusercontent.com/ReadyAPIs-com/curated-us-zips/main/data/us-zips.csv',
    polygons:'https://raw.githubusercontent.com/helenakristina/geopoints_to_markets/master/input/nielsen-dma-markets.geo.json'
  };
  let primaryPromise=null,centroidPromise=null,polygonPromise=null;

  function parseCsvLine(line){
    const out=[];let cur='',quoted=false;
    for(let i=0;i<line.length;i++){
      const ch=line[i];
      if(ch==='"'){
        if(quoted&&line[i+1]==='"'){cur+='"';i++;}
        else quoted=!quoted;
      }else if(ch===','&&!quoted){out.push(cur);cur='';}
      else cur+=ch;
    }
    out.push(cur);return out;
  }
  async function fetchText(url){
    const r=await fetch(url,{cache:'force-cache'});
    if(!r.ok)throw new Error(`Data request failed (${r.status})`);
    return r.text();
  }
  async function loadPrimary(){
    if(primaryPromise)return primaryPromise;
    primaryPromise=(async()=>{
      const text=await fetchText(SOURCES.primary),map=new Map(),lines=text.split(/\r?\n/);
      for(let i=1;i<lines.length;i++){
        if(!lines[i])continue;
        const row=parseCsvLine(lines[i]);
        if(row.length<3)continue;
        const zip=String(row[0]).padStart(5,'0'),code=String(row[1]),name=row[3]||row[2];
        if(/^\d{5}$/.test(zip))map.set(zip,{dma_code:code,dma_name:name});
      }
      return map;
    })();
    return primaryPromise;
  }
  async function loadCentroids(){
    if(centroidPromise)return centroidPromise;
    centroidPromise=(async()=>{
      const text=await fetchText(SOURCES.centroids),map=new Map(),lines=text.split(/\r?\n/);
      const header=parseCsvLine(lines[0]).map(x=>x.trim());
      const zi=header.indexOf('zip_code'),lati=header.indexOf('latitude'),loni=header.indexOf('longitude');
      for(let i=1;i<lines.length;i++){
        if(!lines[i])continue;const row=parseCsvLine(lines[i]);
        const zip=(row[zi]||'').padStart(5,'0'),lat=Number(row[lati]),lon=Number(row[loni]);
        if(/^\d{5}$/.test(zip)&&Number.isFinite(lat)&&Number.isFinite(lon))map.set(zip,{lat,lon});
      }
      return map;
    })();
    return centroidPromise;
  }
  async function loadPolygons(){
    if(polygonPromise)return polygonPromise;
    polygonPromise=(async()=>{
      const r=await fetch(SOURCES.polygons,{cache:'force-cache'});
      if(!r.ok)throw new Error(`DMA boundary request failed (${r.status})`);
      return r.json();
    })();
    return polygonPromise;
  }
  function inRing(point,ring){
    const [x,y]=point;let inside=false;
    for(let i=0,j=ring.length-1;i<ring.length;j=i++){
      const xi=Number(ring[i][0]),yi=Number(ring[i][1]),xj=Number(ring[j][0]),yj=Number(ring[j][1]);
      const hit=((yi>y)!==(yj>y))&&(x<((xj-xi)*(y-yi))/((yj-yi)||1e-15)+xi);
      if(hit)inside=!inside;
    }
    return inside;
  }
  function inPolygon(point,coords){
    if(!coords||!coords.length||!inRing(point,coords[0]))return false;
    for(let i=1;i<coords.length;i++)if(inRing(point,coords[i]))return false;
    return true;
  }
  function geometryContains(point,geometry){
    if(!geometry)return false;
    if(geometry.type==='Polygon')return inPolygon(point,geometry.coordinates);
    if(geometry.type==='MultiPolygon')return geometry.coordinates.some(poly=>inPolygon(point,poly));
    return false;
  }
  function featureMarket(feature){
    const p=feature.properties||{};
    const code=p.dma_code??p.DMA_CODE??p.dma??p.DMA??p.code??p.CODE??feature.id??'';
    const name=p.dma_name??p.DMA_NAME??p.market_name??p.MARKET_NAME??p.market??p.MARKET??p.name??p.NAME??'';
    return {dma_code:String(code||''),dma_name:String(name||'DMA '+code)};
  }
  async function fallbackResolve(zip){
    const centroids=await loadCentroids(),pt=centroids.get(zip);
    if(!pt)return null;
    const geo=await loadPolygons(),features=Array.isArray(geo.features)?geo.features:[];
    const point=[pt.lon,pt.lat];
    for(const f of features){
      if(geometryContains(point,f.geometry)){
        const m=featureMarket(f);
        return {...m,latitude:pt.lat,longitude:pt.lon,method:'centroid-polygon'};
      }
    }
    return null;
  }
  async function resolveZip(rawZip){
    const zip=String(rawZip||'').trim().slice(0,5);
    if(!/^\d{5}$/.test(zip))throw new Error('Enter a valid 5-digit ZIP code.');
    try{
      const direct=(await loadPrimary()).get(zip);
      if(direct)return {zip,...direct,method:'direct-table'};
    }catch(err){console.warn('Primary ZIP/DMA lookup unavailable; trying fallback.',err);}
    const fallback=await fallbackResolve(zip);
    if(fallback)return {zip,...fallback};
    throw new Error('This ZIP code could not be resolved to a DMA.');
  }
  window.NFL_LOCAL_MARKET={resolveZip,sources:SOURCES};
})();
