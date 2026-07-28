// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 { function balanceOf(address) external view returns(uint256); function allowance(address,address) external view returns(uint256); function transfer(address,uint256) external returns(bool); function transferFrom(address,address,uint256) external returns(bool); function approve(address,uint256) external returns(bool); }
interface IV3Factory { function getPool(address,address,uint24) external view returns(address); }
// SwapRouter02 (deployed on Robinhood Chain) omits the legacy per-swap deadline.
interface IV3Router { struct ExactInputSingleParams { address tokenIn; address tokenOut; uint24 fee; address recipient; uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96; } function exactInputSingle(ExactInputSingleParams calldata) external payable returns(uint256); }
interface IV3NPM {
 struct MintParams { address token0; address token1; uint24 fee; int24 tickLower; int24 tickUpper; uint256 amount0Desired; uint256 amount1Desired; uint256 amount0Min; uint256 amount1Min; address recipient; uint256 deadline; }
 struct DecreaseLiquidityParams { uint256 tokenId; uint128 liquidity; uint256 amount0Min; uint256 amount1Min; uint256 deadline; }
 struct CollectParams { uint256 tokenId; address recipient; uint128 amount0Max; uint128 amount1Max; }
 function mint(MintParams calldata) external payable returns(uint256,uint128,uint256,uint256);
 function decreaseLiquidity(DecreaseLiquidityParams calldata) external payable returns(uint256,uint256);
 function collect(CollectParams calldata) external payable returns(uint256,uint256);
 function burn(uint256) external payable;
 function positions(uint256) external view returns(uint96,address,address,address,uint24,int24,int24,uint128,uint256,uint256,uint128,uint128);
}

/// @notice Non-custodial, owner-only atomic USDG/V3 lifecycle executor for Robinhood Chain.
/// @dev The owner keeps each NFT. Existing NFTs work after NPM setApprovalForAll(executor,true).
contract AtomicV3Executor {
 bytes32 public constant CLOSE_MODE=keccak256("REMOVE_COLLECT_ONLY_V1");
 error Unauthorized(); error Paused(); error Reentered(); error WrongChain(); error Expired(); error DeadlineTooFar(); error Invalid(); error PoolMismatch(); error DirtyBalance(); error TransferFailed(); error PullFailed(); error ApproveFailed(); error Slippage(); error TargetDenied(); error SelectorDenied(); error CalldataDenied();
 event Opened(uint256 indexed tokenId,address indexed token,address pool,uint256 usdgIn,uint256 tokenOut);
 event Closed(uint256 indexed tokenId,address indexed token,uint256 settlementAmount,uint256 tokenAmount);
 event PauseSet(bool paused); event OwnershipTransferStarted(address indexed pending); event OwnershipTransferred(address indexed oldOwner,address indexed newOwner); event Rescued(address indexed token,uint256 amount);
 uint256 public constant CHAIN_ID=4663; uint256 public constant MAX_DEADLINE_WINDOW=30 minutes;
 address public immutable USDG; address public immutable FACTORY; address public immutable POSITION_MANAGER; address public immutable SWAP_ROUTER; address public immutable SWAP_TARGET;
 address public owner; address public pendingOwner; bool public paused=true; uint256 private lock=1;
 mapping(bytes4=>bool) public swapSelectorAllowed;
 modifier onlyOwner(){if(msg.sender!=owner)revert Unauthorized();_;} modifier live(){if(block.chainid!=CHAIN_ID)revert WrongChain();if(paused)revert Paused();if(lock!=1)revert Reentered();lock=2;_;lock=1;}
 constructor(address owner_,address usdg,address factory,address npm,address router,address swapTarget){if(owner_==address(0)||usdg==address(0)||factory==address(0)||npm==address(0)||router==address(0)||swapTarget==address(0))revert Invalid();owner=owner_;USDG=usdg;FACTORY=factory;POSITION_MANAGER=npm;SWAP_ROUTER=router;SWAP_TARGET=swapTarget;}
 function setPaused(bool x) external onlyOwner {paused=x;emit PauseSet(x);} function transferOwnership(address x) external onlyOwner {if(x==address(0))revert Invalid();pendingOwner=x;emit OwnershipTransferStarted(x);} function acceptOwnership() external {if(msg.sender!=pendingOwner)revert Unauthorized();emit OwnershipTransferred(owner,msg.sender);owner=msg.sender;pendingOwner=address(0);}
 function setSwapSelector(bytes4 s,bool x) external onlyOwner {if(!paused||s==bytes4(0))revert Invalid();swapSelectorAllowed[s]=x;}
 struct Open { address token; address expectedPool; uint24 fee; int24 tickLower; int24 tickUpper; uint256 usdgAmount; uint256 swapAmount; uint256 minTokenOut; uint256 amount0Min; uint256 amount1Min; uint256 deadline; uint160 sqrtPriceLimitX96; }
 function atomicOpen(Open calldata p) external onlyOwner live returns(uint256 tokenId){_deadline(p.deadline);if(p.token==USDG||p.token==address(0)||p.minTokenOut==0||p.usdgAmount==0||p.swapAmount==0||p.swapAmount>=p.usdgAmount||p.tickLower>=p.tickUpper)revert Invalid();
  (address t0,address t1)=USDG<p.token?(USDG,p.token):(p.token,USDG);if(IV3Factory(FACTORY).getPool(t0,t1,p.fee)!=p.expectedPool||p.expectedPool==address(0))revert PoolMismatch();_clean(p.token);_pull(USDG,msg.sender,p.usdgAmount);_approve(USDG,SWAP_ROUTER,p.swapAmount);
  uint256 beforeTok=IERC20(p.token).balanceOf(address(this));uint256 out=IV3Router(SWAP_ROUTER).exactInputSingle(IV3Router.ExactInputSingleParams(USDG,p.token,p.fee,address(this),p.swapAmount,p.minTokenOut,p.sqrtPriceLimitX96));if(out<p.minTokenOut||IERC20(p.token).balanceOf(address(this))-beforeTok<p.minTokenOut)revert Slippage();_approve(USDG,SWAP_ROUTER,0);
  uint256 a0=IERC20(t0).balanceOf(address(this)); uint256 a1=IERC20(t1).balanceOf(address(this));_approve(t0,POSITION_MANAGER,a0);_approve(t1,POSITION_MANAGER,a1);
  (tokenId,,,)=IV3NPM(POSITION_MANAGER).mint(IV3NPM.MintParams(t0,t1,p.fee,p.tickLower,p.tickUpper,a0,a1,p.amount0Min,p.amount1Min,msg.sender,p.deadline));_approve(t0,POSITION_MANAGER,0);_approve(t1,POSITION_MANAGER,0);_sweep(t0);_sweep(t1);emit Opened(tokenId,p.token,p.expectedPool,p.usdgAmount,out);
 }
 struct Close { uint256 tokenId; address token; address expectedPool; uint24 fee; int24 tickLower; int24 tickUpper; uint128 liquidity; uint256 amount0Min; uint256 amount1Min; uint256 minSwapOut; uint256 minTotalUsdg; uint256 deadline; address swapTarget; bytes swapData; bool burn; }
 function atomicClose(Close calldata p) external onlyOwner live returns(uint256 settlementAmount,uint256 tokenAmount){_deadline(p.deadline);if(p.token==USDG||p.token==address(0)||p.liquidity==0||p.burn)revert Invalid();(address t0,address t1)=USDG<p.token?(USDG,p.token):(p.token,USDG);if(IV3Factory(FACTORY).getPool(t0,t1,p.fee)!=p.expectedPool||p.expectedPool==address(0))revert PoolMismatch();
  (,,address n0,address n1,uint24 f,int24 lo,int24 hi,uint128 liq,,,,)=IV3NPM(POSITION_MANAGER).positions(p.tokenId);if(n0!=t0||n1!=t1||f!=p.fee||lo!=p.tickLower||hi!=p.tickUpper||p.liquidity>liq)revert PoolMismatch();_clean(p.token);uint256 ub=IERC20(USDG).balanceOf(address(this));
  IV3NPM(POSITION_MANAGER).decreaseLiquidity(IV3NPM.DecreaseLiquidityParams(p.tokenId,p.liquidity,p.amount0Min,p.amount1Min,p.deadline));
  // Collect principal and all fees atomically.  No external swap is permitted in close.
  (uint256 got0,uint256 got1)=IV3NPM(POSITION_MANAGER).collect(IV3NPM.CollectParams(p.tokenId,address(this),type(uint128).max,type(uint128).max));
  settlementAmount=t0==USDG?got0:got1;tokenAmount=t0==p.token?got0:got1;
  if(IERC20(USDG).balanceOf(address(this))-ub!=settlementAmount)revert Slippage();
  _sweep(USDG);_sweep(p.token);emit Closed(p.tokenId,p.token,settlementAmount,tokenAmount);
 }
 function rescue(address token) external onlyOwner {if(!paused)revert Invalid();uint256 n=IERC20(token).balanceOf(address(this));_safe(token,owner,n);emit Rescued(token,n);} function _deadline(uint256 d) internal view {if(d==0||d<block.timestamp)revert Expired();if(d>block.timestamp+MAX_DEADLINE_WINDOW)revert DeadlineTooFar();} function _clean(address token) internal view {if(IERC20(USDG).balanceOf(address(this))!=0||IERC20(token).balanceOf(address(this))!=0)revert DirtyBalance();}
 function _swapInput(address target,bytes calldata data,address token,uint256 amount) internal view {if(target!=SWAP_TARGET)revert TargetDenied();if(data.length<4||!swapSelectorAllowed[bytes4(data[:4])])revert SelectorDenied();if(!_contains20(data,bytes20(token))||!_contains20(data,bytes20(USDG))||!_contains32(data,bytes32(amount))||_count20(data,bytes20(address(this)))<2)revert CalldataDenied();}
 function _contains20(bytes calldata data,bytes20 needle) internal pure returns(bool){for(uint i=4;i+20<=data.length;i++){bytes20 x;assembly{x:=calldataload(add(data.offset,i))}if(x==needle)return true;}return false;}
 function _count20(bytes calldata data,bytes20 needle) internal pure returns(uint n){for(uint i=4;i+20<=data.length;i++){bytes20 x;assembly{x:=calldataload(add(data.offset,i))}if(x==needle)n++;}}
 function _contains32(bytes calldata data,bytes32 needle) internal pure returns(bool){for(uint i=4;i+32<=data.length;i++){bytes32 x;assembly{x:=calldataload(add(data.offset,i))}if(x==needle)return true;}return false;}
 function _raw(address t,bytes calldata d) internal {(bool ok,bytes memory r)=t.call(d);if(!ok)assembly{revert(add(r,32),mload(r))}}
 function _pull(address t,address f,uint256 n) internal {uint256 b=IERC20(t).balanceOf(address(this));(bool ok,bytes memory r)=t.call(abi.encodeCall(IERC20.transferFrom,(f,address(this),n)));if(!ok||(r.length!=0&&!abi.decode(r,(bool))))revert PullFailed();if(IERC20(t).balanceOf(address(this))-b!=n)revert PullFailed();} function _safe(address t,address to,uint256 n) internal {if(n!=0)_call(t,abi.encodeCall(IERC20.transfer,(to,n)));} function _approve(address t,address s,uint256 n) internal {bytes memory z=abi.encodeCall(IERC20.approve,(s,0));(bool ok,bytes memory r)=t.call(z);if(!ok||(r.length!=0&&!abi.decode(r,(bool))))revert ApproveFailed();if(n!=0){(ok,r)=t.call(abi.encodeCall(IERC20.approve,(s,n)));if(!ok||(r.length!=0&&!abi.decode(r,(bool))))revert ApproveFailed();}} function _call(address t,bytes memory d) internal {(bool ok,bytes memory r)=t.call(d);if(!ok||(r.length!=0&&!abi.decode(r,(bool))))revert TransferFailed();} function _sweep(address t) internal {_safe(t,owner,IERC20(t).balanceOf(address(this)));if(IERC20(t).balanceOf(address(this))!=0)revert DirtyBalance();}
}
