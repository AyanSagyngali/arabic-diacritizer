"""Шаблоны графики канала «ИСТОРИК» (Motion Graphics ChatCut), перенесены из проекта-примера.

Каждый шаблон создаётся в новом проекте один раз; тексты подставляются через propertyOverrides."""
from __future__ import annotations

FONT_TITLE = "Playfair Display"
FONT_SANS = "Montserrat"

TEMPLATES = {
    "hook_punch": {
        "name": "Hook Punch", "width": 1800, "height": 340, "seconds": 2.5,
        "properties": [
            {"key": "text", "label": "Text", "type": "text", "defaultValue": "ТЕКСТ"},
            {"key": "plateColor", "label": "Plate", "type": "color", "defaultValue": "#C62828"},
            {"key": "textColor", "label": "Text color", "type": "color", "defaultValue": "#FFFFFF"},
            {"key": "fontFamily", "label": "Font", "type": "font", "defaultValue": FONT_SANS},
        ],
        "code": """const Component = ({item}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const s = spring({frame, fps, config:{damping:11, stiffness:170, mass:0.7}});
  const scale = interpolate(s,[0,1],[1.8,1]);
  const slide = interpolate(frame,[0,10],[-100,0],{extrapolateRight:'clamp',extrapolateLeft:'clamp',easing:Easing.out(Easing.cubic)});
  const op = interpolate(frame,[0,4,durationInFrames-8,durationInFrames],[0,1,1,0],{extrapolateLeft:'clamp',extrapolateRight:'clamp'});
  const rootStyle = {width:'100%',height:'100%',display:'flex',alignItems:'center',justifyContent:'center',opacity:op};
  return (
    <div style={rootStyle}>
      <div style={{transform:'scale('+scale+') rotate(-2deg)',maxWidth:1700}}>
        <div style={{transform:'translateX('+slide+'%)',background:item.props.plateColor,padding:'22px 56px',boxShadow:'0 14px 40px rgba(0,0,0,0.55)'}}>
          <div style={{fontFamily:item.props.fontFamily,fontWeight:900,fontSize:100,lineHeight:1.08,color:item.props.textColor,textAlign:'center',textTransform:'uppercase',whiteSpace:'normal',overflowWrap:'break-word',transform:'translateX('+(-slide)+'%)'}}>{item.props.text}</div>
        </div>
      </div>
    </div>
  );
};""",
    },
    "title_reveal": {
        "name": "Title Reveal", "width": 1920, "height": 1080, "seconds": 3.8,
        "properties": [
            {"key": "small", "label": "Top small", "type": "text", "defaultValue": "ВСЯ ИСТОРИЯ"},
            {"key": "title", "label": "Title", "type": "text", "defaultValue": "НАЗВАНИЕ"},
            {"key": "sub", "label": "Bottom small", "type": "text", "defaultValue": ""},
            {"key": "gold", "label": "Gold", "type": "color", "defaultValue": "#D4AF37"},
            {"key": "titleFont", "label": "Title font", "type": "font", "defaultValue": FONT_TITLE},
            {"key": "smallFont", "label": "Small font", "type": "font", "defaultValue": FONT_SANS},
        ],
        "code": """const Component = ({item}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const cl = {extrapolateLeft:'clamp',extrapolateRight:'clamp'};
  const bg = interpolate(frame,[0,14,durationInFrames-14,durationInFrames],[0,1,1,0],cl);
  const a1 = interpolate(frame,[8,22],[0,1],cl);
  const line = interpolate(frame,[16,42],[0,1],{...cl,easing:Easing.out(Easing.cubic)});
  const s = spring({frame:frame-20,fps,config:{damping:14,stiffness:110}});
  const t = interpolate(s,[0,1],[1.25,1]);
  const tOp = interpolate(frame,[20,32],[0,1],cl);
  const a3 = interpolate(frame,[44,60],[0,1],cl);
  const rootStyle = {width:'100%',height:'100%',position:'relative',opacity:bg};
  return (
    <div style={rootStyle}>
      <div style={{position:'absolute',inset:0,background:'radial-gradient(ellipse at center, rgba(0,0,0,0.55) 0%, rgba(0,0,0,0.88) 100%)'}}/>
      <div style={{position:'absolute',left:0,right:0,top:0,bottom:300,display:'flex',flexDirection:'column',alignItems:'center',justifyContent:'center',gap:22,padding:'0 60px'}}>
        <div style={{fontFamily:item.props.smallFont,fontWeight:700,fontSize:40,letterSpacing:14,color:item.props.gold,opacity:a1}}>{item.props.small}</div>
        <div style={{width:900*line,height:4,background:item.props.gold}}/>
        <div style={{fontFamily:item.props.titleFont,fontWeight:800,fontSize:140,lineHeight:1.05,color:'#FFFFFF',textAlign:'center',whiteSpace:'normal',overflowWrap:'break-word',maxWidth:1800,opacity:tOp,transform:'scale('+t+')',textShadow:'0 0 30px '+item.props.gold+', 0 0 70px rgba(212,175,55,0.55)'}}>{item.props.title}</div>
        <div style={{fontFamily:item.props.smallFont,fontWeight:600,fontSize:42,letterSpacing:10,color:item.props.gold,opacity:a3}}>{item.props.sub}</div>
      </div>
    </div>
  );
};""",
    },
    "chapter_card": {
        "name": "Chapter Card", "width": 1920, "height": 220, "seconds": 3.0,
        "properties": [
            {"key": "num", "label": "Chapter label", "type": "text", "defaultValue": "ГЛАВА 1"},
            {"key": "title", "label": "Title", "type": "text", "defaultValue": "Название"},
            {"key": "gold", "label": "Gold", "type": "color", "defaultValue": "#D4AF37"},
            {"key": "titleFont", "label": "Title font", "type": "font", "defaultValue": FONT_TITLE},
            {"key": "numFont", "label": "Label font", "type": "font", "defaultValue": FONT_SANS},
        ],
        "code": """const Component = ({item}) => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const cl = {extrapolateLeft:'clamp',extrapolateRight:'clamp'};
  const w = interpolate(frame,[0,14],[0,1],{...cl,easing:Easing.out(Easing.cubic)});
  const op = interpolate(frame,[0,8,durationInFrames-10,durationInFrames],[0,1,1,0],cl);
  const tx = interpolate(frame,[6,22],[40,0],{...cl,easing:Easing.out(Easing.cubic)});
  const rootStyle = {width:'100%',height:'100%',display:'flex',alignItems:'center',justifyContent:'center',opacity:op};
  return (
    <div style={rootStyle}>
      <div style={{width:'100%',height:'100%',background:'rgba(10,10,12,0.78)',transform:'scaleX('+w+')',transformOrigin:'left center',display:'flex',flexDirection:'column',alignItems:'center',justifyContent:'center',gap:6,borderTop:'3px solid '+item.props.gold,borderBottom:'3px solid '+item.props.gold}}>
        <div style={{transform:'translateX('+tx+'px)',display:'flex',flexDirection:'column',alignItems:'center',gap:6}}>
          <div style={{fontFamily:item.props.numFont,fontWeight:700,fontSize:36,letterSpacing:12,color:item.props.gold}}>{item.props.num}</div>
          <div style={{fontFamily:item.props.titleFont,fontWeight:700,fontSize:82,lineHeight:1.05,color:'#FFFFFF',textAlign:'center',whiteSpace:'normal',overflowWrap:'break-word',maxWidth:1700}}>{item.props.title}</div>
        </div>
      </div>
    </div>
  );
};""",
    },
    "name_title": {
        "name": "Name Title", "width": 1100, "height": 160, "seconds": 3.5,
        "properties": [
            {"key": "name", "label": "Name", "type": "text", "defaultValue": "Имя"},
            {"key": "caption", "label": "Caption", "type": "text", "defaultValue": "описание"},
            {"key": "gold", "label": "Gold", "type": "color", "defaultValue": "#D4AF37"},
            {"key": "nameFont", "label": "Name font", "type": "font", "defaultValue": FONT_TITLE},
            {"key": "capFont", "label": "Caption font", "type": "font", "defaultValue": FONT_SANS},
        ],
        "code": """const Component = ({item}) => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const cl = {extrapolateLeft:'clamp',extrapolateRight:'clamp'};
  const bar = interpolate(frame,[0,12],[0,1],{...cl,easing:Easing.out(Easing.cubic)});
  const x = interpolate(frame,[4,20],[-60,0],{...cl,easing:Easing.out(Easing.cubic)});
  const op = interpolate(frame,[4,16,durationInFrames-12,durationInFrames],[0,1,1,0],cl);
  const barOp = interpolate(frame,[durationInFrames-12,durationInFrames],[1,0],cl);
  const rootStyle = {width:'100%',height:'100%',display:'flex',alignItems:'center',gap:22};
  return (
    <div style={rootStyle}>
      <div style={{width:8,height:150*bar,background:item.props.gold,flexShrink:0,opacity:barOp}}/>
      <div style={{display:'flex',flexDirection:'column',gap:4,opacity:op,transform:'translateX('+x+'px)',textShadow:'0 3px 14px rgba(0,0,0,0.9)'}}>
        <div style={{fontFamily:item.props.nameFont,fontWeight:700,fontSize:78,lineHeight:1.05,color:'#FFFFFF',whiteSpace:'normal',overflowWrap:'break-word'}}>{item.props.name}</div>
        <div style={{fontFamily:item.props.capFont,fontWeight:600,fontSize:32,color:item.props.gold,letterSpacing:2}}>{item.props.caption}</div>
      </div>
    </div>
  );
};""",
    },
    "date_stamp": {
        "name": "Date Stamp", "width": 440, "height": 230, "seconds": 3.3,
        "properties": [
            {"key": "year", "label": "Year", "type": "text", "defaultValue": "1723"},
            {"key": "caption", "label": "Caption", "type": "text", "defaultValue": "СОБЫТИЕ"},
            {"key": "red", "label": "Red", "type": "color", "defaultValue": "#B71C1C"},
            {"key": "paper", "label": "Parchment", "type": "color", "defaultValue": "#EBDDB5"},
            {"key": "yearFont", "label": "Year font", "type": "font", "defaultValue": FONT_SANS},
        ],
        "code": """const Component = ({item}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const cl = {extrapolateLeft:'clamp',extrapolateRight:'clamp'};
  const s = spring({frame,fps,config:{damping:9,stiffness:180,mass:0.6}});
  const sc = interpolate(s,[0,1],[1.7,1]);
  const rot = interpolate(s,[0,1],[8,-3]);
  const op = interpolate(frame,[0,4,durationInFrames-10,durationInFrames],[0,1,1,0],cl);
  const rootStyle = {width:'100%',height:'100%',display:'flex',alignItems:'center',justifyContent:'center',opacity:op};
  return (
    <div style={rootStyle}>
      <div style={{width:400,height:190,transform:'scale('+sc+') rotate('+rot+'deg)',background:item.props.paper,border:'8px solid '+item.props.red,boxShadow:'0 10px 30px rgba(0,0,0,0.55)',display:'flex',flexDirection:'column',alignItems:'center',justifyContent:'center',gap:2}}>
        <div style={{fontFamily:item.props.yearFont,fontWeight:900,fontSize:item.props.year.length > 5 ? 64 : 104,lineHeight:1,color:item.props.red}}>{item.props.year}</div>
        <div style={{fontFamily:item.props.yearFont,fontWeight:700,fontSize:28,letterSpacing:5,color:'#3B2A17',textAlign:'center'}}>{item.props.caption}</div>
      </div>
    </div>
  );
};""",
    },
}
