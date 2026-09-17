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
  particles=Array.from({length:count},()=>({x:Math.random()*W,y:Math.random()*H,vx:(Math.random()-.5)*.24,vy:(Math.random()-.5)*.24,r:.7+Math.random()*1.8,phase:Math.random()*Math.PI*2,speed:.008+Math.random()*.018,glow:.35+Math.random()*.65}));
}

function draw(){
  ctx.clearRect(0,0,W,H);
  for(const p of particles){
    p.x+=p.vx;p.y+=p.vy;p.phase+=p.speed;
    if(p.x<-20)p.x=W+20;if(p.x>W+20)p.x=-20;if(p.y<-20)p.y=H+20;if(p.y>H+20)p.y=-20;
    const alpha=p.glow*(.35+.65*((Math.sin(p.phase)+1)/2));
    const gradient=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,p.r*9);
    gradient.addColorStop(0,`rgba(0,229,255,${alpha})`);gradient.addColorStop(.18,`rgba(0,229,255,${alpha*.45})`);gradient.addColorStop(1,'rgba(0,229,255,0)');
    ctx.beginPath();ctx.fillStyle=gradient;ctx.arc(p.x,p.y,p.r*9,0,Math.PI*2);ctx.fill();
    ctx.beginPath();ctx.fillStyle=`rgba(220,255,255,${Math.min(1,alpha+.12)})`;ctx.arc(p.x,p.y,p.r,0,Math.PI*2);ctx.fill();
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

function openImpressum(){modal.classList.add('open');modal.setAttribute('aria-hidden','false');document.getElementById('close-impressum').focus();}
function closeImpressum(){modal.classList.remove('open');modal.setAttribute('aria-hidden','true');}

document.getElementById('open-impressum').addEventListener('click',openImpressum);
document.getElementById('close-impressum').addEventListener('click',closeImpressum);
modal.addEventListener('click',event=>{if(event.target===modal)closeImpressum();});
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeImpressum();});
document.getElementById('loading-logo').addEventListener('click',openImpressum);
document.getElementById('final-logo').addEventListener('click',openImpressum);

/* Neptune / KaiserChat: same visual position as the top-left planet, reduced to 80% and fully clickable. */
const neptuneStyle=document.createElement('style');
neptuneStyle.textContent=`
#experience::before{width:clamp(240px,24vw,364px)!important;height:clamp(240px,24vw,364px)!important}
#experience::after{width:clamp(164px,16vw,240px)!important;height:clamp(164px,16vw,240px)!important}
#neptune-link{position:absolute;z-index:4;top:-72px;left:-78px;width:clamp(240px,24vw,364px);height:clamp(240px,24vw,364px);display:block;cursor:pointer;text-decoration:none;border-radius:50%;outline:none}
#neptune-link:hover{filter:drop-shadow(0 0 18px rgba(0,229,255,.28))}
#neptune-link:focus-visible{outline:1px solid rgba(0,229,255,.55);outline-offset:6px}
@media(max-width:600px){#experience::before{top:-52px!important;left:-58px!important;width:clamp(188px,46.4vw,264px)!important;height:clamp(188px,46.4vw,264px)!important}#experience::after{top:14px!important;left:14px!important;width:clamp(128px,31.2vw,180px)!important;height:clamp(128px,31.2vw,180px)!important}#neptune-link{top:-52px;left:-58px;width:clamp(188px,46.4vw,264px);height:clamp(188px,46.4vw,264px)}}
@media(max-height:560px) and (orientation:landscape){#experience::before{top:-48px!important;left:-58px!important;width:232px!important;height:232px!important}#experience::after{top:10px!important;left:10px!important;width:164px!important;height:164px!important}#neptune-link{top:-48px;left:-58px;width:232px;height:232px}}
`;
document.head.appendChild(neptuneStyle);
const neptuneLink=document.createElement('a');
neptuneLink.id='neptune-link';
neptuneLink.href='https://kaiserchat.vercel.app/';
neptuneLink.target='_blank';
neptuneLink.rel='noopener noreferrer';
neptuneLink.setAttribute('aria-label','KaiserChat öffnen');
neptuneLink.title='KaiserChat öffnen';
experience.appendChild(neptuneLink);

function load(now){
  const progress=Math.min(100,(now-start)/duration*100);
  progressBar.style.width=progress+'%';
  progressPercent.textContent=Math.round(progress)+'%';
  if(progress<100){requestAnimationFrame(load);return;}
  setTimeout(()=>{loading.classList.add('done');experience.classList.add('visible');if(!raf)raf=requestAnimationFrame(draw);},180);
}

requestAnimationFrame(load);
