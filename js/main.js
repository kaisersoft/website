const canvas=document.getElementById('fireflies');
const ctx=canvas.getContext('2d');
let W,H,D,particles=[],raf;

function resize(){
  D=Math.min(window.devicePixelRatio||1,2);
  W=window.innerWidth;
  H=window.innerHeight;
  canvas.width=W*D;
  canvas.height=H*D;
  canvas.style.width=W+'px';
  canvas.style.height=H+'px';
  ctx.setTransform(D,0,0,D,0,0);
  createParticles();
}

function createParticles(){
  const count=Math.max(28,Math.min(110,Math.round(W*H/14500)));
  particles=Array.from({length:count},()=>({
    x:Math.random()*W,
    y:Math.random()*H,
    vx:(Math.random()-.5)*.24,
    vy:(Math.random()-.5)*.24,
    r:.7+Math.random()*1.8,
    phase:Math.random()*Math.PI*2,
    speed:.008+Math.random()*.018,
    glow:.35+Math.random()*.65
  }));
}

function draw(){
  ctx.clearRect(0,0,W,H);
  for(const p of particles){
    p.x+=p.vx;
    p.y+=p.vy;
    p.phase+=p.speed;
    if(p.x<-20)p.x=W+20;
    if(p.x>W+20)p.x=-20;
    if(p.y<-20)p.y=H+20;
    if(p.y>H+20)p.y=-20;

    const alpha=p.glow*(.35+.65*((Math.sin(p.phase)+1)/2));
    const gradient=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,p.r*9);
    gradient.addColorStop(0,`rgba(0,229,255,${alpha})`);
    gradient.addColorStop(.18,`rgba(0,229,255,${alpha*.45})`);
    gradient.addColorStop(1,'rgba(0,229,255,0)');

    ctx.beginPath();
    ctx.fillStyle=gradient;
    ctx.arc(p.x,p.y,p.r*9,0,Math.PI*2);
    ctx.fill();

    ctx.beginPath();
    ctx.fillStyle=`rgba(220,255,255,${Math.min(1,alpha+.12)})`;
    ctx.arc(p.x,p.y,p.r,0,Math.PI*2);
    ctx.fill();
  }
  raf=requestAnimationFrame(draw);
}

window.addEventListener('resize',resize,{passive:true});
resize();

const loading=document.getElementById('loading');
const experience=document.getElementById('experience');
const progressBar=document.getElementById('progress-bar');
const progressPercent=document.getElementById('progress-percent');
const modal=document.getElementById('impressum-modal');
const start=performance.now();
const duration=3600;

function openImpressum(){
  modal.classList.add('open');
  modal.setAttribute('aria-hidden','false');
  document.getElementById('close-impressum').focus();
}

function closeImpressum(){
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden','true');
}

document.getElementById('open-impressum').addEventListener('click',openImpressum);
document.getElementById('close-impressum').addEventListener('click',closeImpressum);
modal.addEventListener('click',event=>{
  if(event.target===modal)closeImpressum();
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape')closeImpressum();
});
document.getElementById('loading-logo').addEventListener('click',openImpressum);
document.getElementById('final-logo').addEventListener('click',openImpressum);

function load(now){
  const progress=Math.min(100,(now-start)/duration*100);
  progressBar.style.width=progress+'%';
  progressPercent.textContent=Math.round(progress)+'%';

  if(progress<100){
    requestAnimationFrame(load);
    return;
  }

  setTimeout(()=>{
    loading.classList.add('done');
    experience.classList.add('visible');
    if(!raf)raf=requestAnimationFrame(draw);
  },180);
}

requestAnimationFrame(load);
