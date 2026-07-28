require("@nomicfoundation/hardhat-toolbox");
module.exports={solidity:{version:"0.8.24",settings:{optimizer:{enabled:true,runs:200},viaIR:true}},networks:{hardhat:{chainId:4663}},paths:{tests:"./contract-test",cache:"./.hardhat-cache",artifacts:"./.hardhat-artifacts"}};
